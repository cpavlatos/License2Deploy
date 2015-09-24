#!/usr/bin/python

import logging
from sys import exit, argv
from time import sleep, time
from AWSConn import AWSConn

class RollingDeploy(object):

  def __init__(self, env=None, project=None, buildNum=None, ami_id=None, profile_name='default', regions_conf='/opt/License2Deploy/regions.yml'):
    self.env = env
    self.project = project.replace('-','')
    self.buildNum = buildNum
    self.ami_id = ami_id
    self.profile_name = profile_name
    self.regions_conf = regions_conf
    self.environments = AWSConn.load_config(self.regions_conf).get(self.env)
    self.region = AWSConn.determine_region(self.environments)
    self.conn_ec2 = AWSConn.aws_conn_ec2(self.region, self.profile_name)
    self.conn_elb = AWSConn.aws_conn_elb(self.region, self.profile_name)
    self.conn_auto = AWSConn.aws_conn_auto(self.region, self.profile_name)
    self.exit_error_code = 2

  def get_ami_id_state(self, ami_id):
    try:
      ami_obj = self.conn_ec2.get_all_images(image_ids=ami_id)
    except Exception as e:
      logging.error("Unable to get ami-id, please investigate: {0}".format(e))
      exit(self.exit_error_code)
    return ami_obj[0]

  def wait_ami_availability(self, ami_id, timeout=5):
    ''' Timeout should be in minutes '''
    timeout = time() + 60*timeout
    while True:
      ami = self.get_ami_id_state(ami_id).state
      if time() < timeout and ami == 'available':
        logging.info("AMI {0} is ready".format(ami_id))
        return True
      elif time() > timeout:
        logging.error("AMI {0} is not ready after {1} minutes, please investigate".format(ami_id, timeout))
        exit(self.exit_error_code)
      else:
        logging.warning("AMI {0} is not ready yet, retrying in 30 seconds".format(ami_id))
        sleep(timeout)
      
  def get_group_info(self, group_name=None):
    try:
      if group_name:
        rslt = self.conn_auto.get_all_groups([group_name])
        if not rslt:
          raise Exception("Bad Group: {0}".format(group_name))
        return rslt
      else:
        return self.conn_auto.get_all_groups()
    except Exception as e:
      logging.error("Unable to pull down autoscale group: {0}".format(e))
      exit(self.exit_error_code)

  def get_autoscale_group_name(self):
    ''' Search for project in autoscale groups and return autoscale group name '''
    proj_name = next((instance.name for instance in filter(lambda n: n.name, self.get_group_info()) if self.project in instance.name), None)
    return proj_name

  def get_lb(self):
    try:
      return next(n.name for n in self.conn_elb.get_all_load_balancers() if self.project in str(n.name))
    except Exception as e:
      logging.error("Unable to pull down ELB info: {0}".format(e))
      exit(self.exit_error_code)

  def calculate_autoscale_desired_instance_count(self, group_name, desired_state):
    ''' Search via specific autoscale group name to return modified desired instance count '''
    try:
      cur_count = int(self.get_group_info(group_name)[0].desired_capacity)
      if desired_state == 'increase':
        new_count = self.double_autoscale_instance_count(cur_count)
      elif desired_state == 'decrease':
        new_count = self.decrease_autoscale_instance_count(cur_count)
      logging.info("Current desired count was changed from {0} to {1}".format(cur_count, new_count))
      return new_count
    except UnboundLocalError as u:
      logging.error("Please make sure the desired_state is set to either increase or decrease: {0}".format(u))
      exit(self.exit_error_code)
 
  def double_autoscale_instance_count(self, count):
    ''' Multiply current count by 2 '''
    return count * 2

  def decrease_autoscale_instance_count(self, count):
    ''' Divide current count in half '''
    return count / 2

  def set_autoscale_instance_desired_count(self, new_count, group_name):
    ''' Increase desired count by double '''
    try:
      logging.info("Set autoscale capacity for {0} to {1}".format(group_name, new_count))
      self.conn_auto.set_desired_capacity(group_name, new_count)
      return True
    except Exception as e:
      logging.error("Unable to update desired count, please investigate error: {0}".format(e))
      exit(self.exit_error_code)
      
  def get_all_instance_ids(self, group_name):
    ''' Gather Instance id's of all instances in the autoscale group '''
    instances = [ i for i in self.get_group_info(group_name)[0].instances ]
    id_list = []
    for instance_id in instances:
      id_list.append(instance_id.instance_id)

    logging.info("List of all Instance ID's in {0}: {1}".format(group_name, id_list))
    return id_list

  def get_instance_ids_by_requested_build_tag(self, id_list, build):
    ''' Gather Instance id's of all instances in the autoscale group '''
    reservations = self.conn_ec2.get_all_reservations()
    new_instances = []
    for instance_id in id_list:
      rslt = [inst for r in reservations for inst in r.instances if 'BUILD' in inst.tags and inst.id == instance_id]
      for new_id in rslt:
        if new_id.tags['BUILD'] == str(build):
          new_instances.append(instance_id)
    
    if new_instances:
      logging.info("New Instance List: {0}".format(new_instances))
      return new_instances
    else:
      logging.error("New Instance List is empty, something went wrong")
      exit(self.exit_error_code)
    
  def wait_for_new_instances(self, instance_ids, retry=9, wait_time=30):
    ''' Monitor new instances that come up and wait until they are ready '''
    for instance in instance_ids:
      count = 0
      health = []
      while count <= retry and (len(health) < 2):
        instanceStatus = self.conn_ec2.get_all_instance_status(instance)
        for state in instanceStatus:
          health = [x for x in [str(state.system_status.status), str(state.instance_status.status)] if x == "ok"]
          if (len(health) < 2):
            logging.warning("{0} is not in a fully working state yet".format(instance))
            sleep(wait_time)
            count += 1
            if count > retry:
              logging.error("{0} has not reached a valid healthy state".format(instance))
              exit(self.exit_error_code)
          else:
            logging.info("{0} is in a healthy state. Moving on...".format(instance))

  def lb_healthcheck(self, new_ids, retry=10, wait_time=10):
    ''' Confirm that the healthchecks report back OK in the LB. '''
    lb = self.get_lb()
    inst_length = len(new_ids)
    for inst_id in range(inst_length):
      count = 0
      instance_id = self.conn_elb.describe_instance_health(lb)[inst_id]
      while instance_id.state != 'InService':
        logging.warning("Load balancer healthcheck is returning {0} for {1}. Retrying after 10 seconds. Count == {2}".format(instance_id.state, instance_id.instance_id, count))
        instance_id = self.conn_elb.describe_instance_health(lb)[inst_id]
        count = (count + 1)
        if instance_id.state != 'InService' and (count >= retry):
          logging.error("Load balancer healthcheck returning {0} for {1} and has exceeded the timeout threshold set. Please roll back.".format(instance_id.state, instance_id.instance_id)) 
          exit(self.exit_error_code)
        sleep(wait_time)
      logging.info("ELB healthcheck OK == {0}: {1}".format(instance_id.instance_id, instance_id.state))
    return True

  def confirm_lb_has_only_new_instances(self, wait_time=30):
    ''' Confirm that only new instances with the current build tag are in the load balancer '''
    sleep(wait_time) # Allotting time for the instances to shut down
    lb = self.get_lb()
    instance_ids = self.conn_elb.describe_instance_health(lb)
    for instance in instance_ids:
      build = self.conn_ec2.get_all_reservations(instance.instance_id)[0].instances[0].tags['BUILD']
      if build != self.buildNum:
        logging.error("There is still an old instance in the ELB: {0}. Please investigate".format(instance))
        exit(self.exit_error_code)
    logging.info("Deployed instances {0} to ELB: {1}".format(instance_ids, lb))
    return instance_ids

  def healthcheck_new_instances(self, group_name): # pragma: no cover
    ''' Healthchecking new instances to ensure deployment was successful '''
    instance_ids = self.get_all_instance_ids(group_name)
    new_instance_ids = self.get_instance_ids_by_requested_build_tag(instance_ids, self.buildNum)
    self.wait_for_new_instances(new_instance_ids) #Wait for new instances to be up and ready
    self.lb_healthcheck(new_instance_ids) #Once instances are ready, healthcheck. If successful, decrease desired count.

  def deploy(self): # pragma: no cover
    ''' Rollin Rollin Rollin, Rawhide! '''
    group_name = self.get_autoscale_group_name()
    self.wait_ami_availability(self.ami_id)
    logging.info("Build #: {0} ::: Autoscale Group: {1}".format(self.buildNum, group_name))
    self.set_autoscale_instance_desired_count(self.calculate_autoscale_desired_instance_count(group_name, 'increase'), group_name)
    logging.info("Sleeping for 240 seconds to allow for instances to spin up")
    sleep(240) #Need to wait until the instances come up in the load balancer
    self.healthcheck_new_instances(group_name)
    self.set_autoscale_instance_desired_count(self.calculate_autoscale_desired_instance_count(group_name, 'decrease'), group_name)
    self.confirm_lb_has_only_new_instances()
    logging.info("Deployment Complete!")

def check_args(): # pragma: no cover
  if (len(argv) < 5):
    logging.error("Please enter five arguments: {0} {1} {2} {3} {4} {5}".format(argv[0], 'env', 'project', 'buildnumber', 'ami-id', 'profile'))
    exit(2) #Exiting with error code

def setup_logging(): # pragma: no cover
  logging.basicConfig(format='%(asctime)s: %(levelname)s: %(message)s',level=logging.INFO)
  logging.info("Begin Logging...")

def main(): # pragma: no cover
  setup_logging()
  check_args()
  l = RollingDeploy(argv[1], argv[2], argv[3], argv[4], argv[5])
  l.deploy()
  
if __name__ == "__main__": # pragma: no cover
    main()