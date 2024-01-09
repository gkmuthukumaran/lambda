import boto3
import datetime
from dateutil.parser import parse
import botocore

ec2_region = 'sa-so-1'
# ec2_region = 'us-east-1'
ec2_client = boto3.client('ec2',region_name=ec2_region)
ec2_response = ec2_client.describe_instances() 
age_days = 1

#Notify if there are no instances to generate ami from.
def check_running_stopped():
    count  = 0
    for reservation in (ec2_response['Reservations']):
        for instances in reservation['Instances']:
            if instances['State']['Name'] == 'stopped' or instances['State']['Name'] == 'running' or instances['State']['Name'] == 'stopping':
                        count+= 1
    if count == 0:
        print("There are no instances running or stopped to generate amis/images. No AMIs will be generated")


#Obtained instance IDs and tags as a dictionary
def get_instanceId_nameTags(response):
    ids_tags = {}
    for reservation in (response['Reservations']):
        for instance_id in reservation['Instances']:
            # if instance_id['State']['Name'] == 'stopped' or instance_id['State']['Name'] == 'running' or instance_id['State']['Name'] == 'stopping':
                for tags in instance_id['Tags']:
                    if tags['Key'] == 'Name' and tags['Value']:
                            ids_tags[instance_id['InstanceId']] = tags['Value']
                    else:
                        ids_tags[instance_id['InstanceId']] = "random"
    return ids_tags

#Generated a date+time string to append to AMI names
def timestamp():
    return "_" + datetime.datetime.now().strftime('%Y-%m-%d-%H_%M_%S')
    
def publish_message(error_code):
    sns_arn = 'arn:aws:sns:us-east-1:814109103016:Failed_AMI_Tasks'
    snsclient = boto3.client('sns')
    try:
        # Sending the notification...
        snsclient.publish(
            TargetArn=sns_arn,
            Subject="Issue Creating or Deleting AMI",
            Message=error_code
        )
    except botocore.exceptions.ClientError as error:
        print(error.response['Error']['Code'] + " exception occured while publishing SNS notification ")

#Assigned the EC2 tags to the AMI and appended timestamps to the AMI names
def create_ami(instance_id,tag_value):
        try:
            image_response = ec2_client.create_image(Description='This is ami for ' + instance_id,InstanceId=instance_id,Name=tag_value + timestamp(),NoReboot=True,
                TagSpecifications=[
                    {
                        'ResourceType': 'image',
                        'Tags': [
                            {
                                'Key': 'Name',
                                'Value': tag_value
                            },
                        ]
                    },
                    {
                        'ResourceType': 'snapshot',
                        'Tags': [
                            {
                                'Key': 'Name',
                                'Value': tag_value
                            },
                        ]
                    },        
                ]
            )
            if image_response['ResponseMetadata']['HTTPStatusCode'] == 200:
                print("Created ami: " + image_response['ImageId'])
                return image_response
            else:
                print("Issue creating ami from instance " + instance_id)
                publish_message("Issue creating ami from instance " + instance_id)
        except botocore.exceptions.ClientError as error:
            if error.response['Error']['Code'] == 'InvalidParameterValue':
                print(instance_id + " is not in a 'running' or 'stopping' or 'stopped' state")
                publish_message(instance_id + " is not in a 'running' or 'stopping' or 'stopped' state")
            else:
                print(error.response['Error']['Code'] + " exception error occured while creating an image from instance: " + instance_id)
                publish_message(error.response['Error']['Code'])

#get all ami ids created by me
def get_all_ami_ids():
    ami_ids = set()
    images = ec2_client.describe_images(Owners=['self',])
    for ami_id in images['Images']:
      ami_ids.add(ami_id['ImageId'])
    return ami_ids

#get all ami ids currently used by non-terminated instances
def get_instance_ami_ids(ec2_response):
    instance_amis = set()
    for reservation in (ec2_response['Reservations']):
        for instance_id in reservation['Instances']:
            if instance_id['State']['Name'] == 'stopped' or instance_id['State']['Name'] == 'running':
                instance_amis.add(instance_id['ImageId'])
    return instance_amis

#get a list/set of unused ami ids 
def get_unused_ami_ids(all_ami,used_ami):
    return all_ami - used_ami

#delete unused amis older that x number of days
def cleanup_unused_ami(unused_ami):
    for image_id in unused_ami:
        a = ec2_client.describe_images(ImageIds=[image_id])['Images'][0]['CreationDate']
        day_delta = datetime.datetime.now().date() - parse(a).replace(tzinfo=None).date()
        try:
            if day_delta.days <= age_days:
                image_dereg_resp = ec2_client.deregister_image(ImageId=image_id)
                if image_dereg_resp['ResponseMetadata']['HTTPStatusCode'] == 200:
                    print("Deleted ami: " + image_id)
                else:
                    print("Ami: " + image_id + " was not successfully deleted")
                    publish_message("Ami: " + image_id + " was not successfully deleted")
        except botocore.exceptions.ClientError as error:
            print(error.response['Error']['Code'] + " exception occured while deleting ami: " + image_id)
            publish_message(error.response['Error']['Code'] + " exception occured while deleting ami: " + image_id)
        continue

#delete unused snapshots older that x number of days
def cleanup_unused_snapshot(snapshots):
    if len(snapshots['Snapshots']) == 0:
        print ("No EBS snapshots were found")
    else:
        for snapshot in snapshots['Snapshots']:
            day_delta = datetime.datetime.now().date() - snapshot['StartTime'].date()
            try:
                if day_delta.days <= age_days:
                    delete_response = ec2_client.delete_snapshot(SnapshotId=snapshot['SnapshotId'])
                    if delete_response['ResponseMetadata']['HTTPStatusCode'] == 200:
                        print("Deleted snapshot: " + snapshot['SnapshotId'])
                    else:
                        print("Snapshot " + snapshot['SnapshotId'] + " was not deleted successfully")
            except botocore.exceptions.ClientError as error:
                    if error.response['Error']['Code'] == 'InvalidSnapshot.InUse':
                        print("Skipped this snapshot which is in use: " + snapshot['SnapshotId'] )
                    else:
                        print(error.response['Error']['Code'] + " exception error occured while deleting " + snapshot['SnapshotId'])
            continue


# Main Function
def lambda_handler(event, context):
    #Check if any images can be created based on presence of running or stopped instances
    check_running_stopped()

    #Create and label images using obtained instance ids and tags
    for ec2_id, ec2_nametag in get_instanceId_nameTags(ec2_response).items():
      create_ami(ec2_id,ec2_nametag)

    #Obtain a list of unused AMIs
    unused_ami_ids = get_unused_ami_ids(get_all_ami_ids(),get_instance_ami_ids(ec2_response))

    #Cleanup unused AMIs and snapshots over x days old
    cleanup_unused_ami(unused_ami_ids)
    cleanup_unused_snapshot(ec2_client.describe_snapshots(OwnerIds=['self']))


