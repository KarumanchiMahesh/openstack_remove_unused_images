#!/usr/bin/python
import argparse
import logging
import pexpect
import yaml
import re
from datetime import datetime

p = re.compile("u'(\w+-\w+-\w+-\w+-\w+)'")
details = yaml.load(open('/root/Virtu/deleteimages/clouds.yaml'))
scaleio_clouds = ['Merlin', 'Milton', 'Kennedy', 'William','Angelina']
#scaleio_clouds = ['Merlin', 'Kennedy', 'William', 'Milton', 'Angelina']
nonscaleio_clouds = ['Nikola', 'Bill', 'Cooper', 'Marilyn', 'Net2', 'Newhope', 'Andromeda', 'Skynet']
#clouds = ['Andromeda', 'Angelina', 'William', 'Bill', 'Cooper', 'Marilyn', 'Net2', 'Newhope', 'Nikola', 'Skynet', 'Merlin', 'Milton', 'Kennedy']
clouds = scaleio_clouds + nonscaleio_clouds

def check_time(child, image_id):
    logging.debug("Checking the age of the image %s" % image_id)
    image_show = send_command(child, "glance image-show %s | grep 'updated_at'" % image_id)
    image_updated_time = re.search("(\d+-\d+-\d+T\d+:\d+:\d\d)", image_show)
    if image_updated_time:
        image_updated_time = image_updated_time.group(0)
        delta = datetime.today() - datetime.strptime(image_updated_time, '%Y-%m-%dT%H:%M:%S')
        age_in_hours, remainder = divmod(delta.seconds, 3600)
        age_in_hours += delta.days * 24
        return age_in_hours
    else:
        return None


def connect(node):
    logging.info("Connecting to cloud {}".format(node))
    ssh_newkey = 'Are you sure you want'
    refused = 'Connection refused'
    child = pexpect.spawn("ssh -o StrictHostKeyChecking=no ceeadm@%s" % details[node]['cic_vip'])
    response = child.expect([pexpect.TIMEOUT, ssh_newkey, pexpect.EOF, '[P|p]assword', refused])
    if response == 0 or response == 2:
        logging.info("Failed at ssh status : Error connecting")
        return
    elif response == 1:
        child.sendline('yes')
        result = child.expect([pexpect.TIMEOUT, pexpect.EOF, '[P|p]assword'])
        if result == 0 or result == 1:
            logging.info('Error while connecting after entering yes to RSA prompt')
            return
    child.sendline(details[node]['Password'])
    output = child.expect([pexpect.TIMEOUT, pexpect.EOF, ':~\$'])
    if output == 2:
        return child
    else:
        return


def send_command(child, command):
    logging.debug("executing command %s " % (command))
    child.sendline(command)
    output=child.expect([pexpect.TIMEOUT, pexpect.EOF, ':~\$'])
    if output == 2:    
	    return child.before
    else:
        logging.debug("ERROR WHILE EXECUTING COMMAND %s " % (command))
        return


def source_openrc(child):
    logging.debug("Sourcing openrc file")
    send_command(child, 'sudo cp /root/openrc .')
    send_command(child, 'sudo chmod 777 openrc')
    send_command(child, 'source openrc')
    return 0


def get_del_image_ids(child):
    source_openrc(child)
    glance_image_ids = send_command(child, "glance image-list | grep -v 'iPXE\|TestVM\|atlas' | cut -d '|' -f2 ")
    glance_image_ids = re.findall("(\w+-\w+-\w+-\w+-\w+)", glance_image_ids)
    logging.info("All image ids in glance excluding iPXE and TestVM are {}".format(glance_image_ids))
    VM_ids = send_command(child, "nova list --all-tenants --fields image | cut -d '|' -f 2")
    VM_ids = re.findall("(\w+-\w+-\w+-\w+-\w+)", VM_ids)
    logging.debug("The ids of VM's in Nova are {}".format(VM_ids))
    nova_list = send_command(child, 'nova list --all-tenants --fields image')
    nova_image_ids = re.findall("u'(\w+-\w+-\w+-\w+-\w+)'", nova_list)
    nova_image_ids = list(set(nova_image_ids))
    logging.info("Image ids which are in use in Nova are {}".format(nova_image_ids))
    del_images = [image for image in glance_image_ids if image not in nova_image_ids]
    return del_images, VM_ids


def main():
    logging.info("Started to clean images in GLance for cloud %s" % clouds)

    for cloud in clouds:
        delete_images = []
        child = connect(cloud)
        if child:
            storage_before = send_command(child, "df -h /dev/mapper/image-glance")
            logging.info("before deleting images {}".format(storage_before))
            if cloud in nonscaleio_clouds:
                logging.info("Deleting images on non-scaleio cloud  %s " % cloud)
                delete_images, VM_ids = get_del_image_ids(child)
            if cloud in scaleio_clouds:
                logging.info("Deleting images on scaleio cloud %s" % cloud)
                del_images, VM_ids = get_del_image_ids(child)
                cinder_images = []
                for VM_id in VM_ids:
                    volume_show = send_command(child, "nova volume-attachments %s | cut -d '|' -f5" % VM_id)
                    volume_ids = re.findall('[\^\n]\s*(\w+-\w+-\w+-\w+-\w+)', volume_show) # excludes the match on command
                    logging.debug("The volume id/ids associated to VM %s is/are %s" % (VM_id, volume_ids))
                    for vol_id in volume_ids:
                        cinder_metadata = send_command(child, "cinder image-metadata-show %s | "
                                                              "grep 'image_id' | cut -d '|' -f3" % vol_id)
                        if "ERROR: GlanceMetadataNotFound:" not in cinder_metadata:
                            cinder_image_ids = re.findall('[\^\n]\s*(\w+-\w+-\w+-\w+-\w+)', cinder_metadata)
                            logging.debug("The image id associated with VM %s is "
                                          "%s (in cinder scaleio)" % (VM_id, cinder_image_ids))
                            cinder_images.append(cinder_image_ids[0]) #Since only one image can be attached to a instance atmost
                logging.debug("The images in cinder and used by nova are {}; number of images {}"
                                  .format(cinder_images, len(cinder_images)))
                logging.info("The images that can be deleted before checking in scaleio are {}".format(del_images))
                delete_images = [image for image in del_images if image not in cinder_images]
            logging.info("The image that can be deleted are after checking in cinder are {}"
                             .format(delete_images))
            for image_id in delete_images:
                hours_old = check_time(child, image_id)
                if hours_old > 6:
                    logging.info("Deleting image %s as it is older than 6 hours" % image_id)
                    send_command(child, "glance image-update %s --protected False" % image_id)
                    send_command(child, "glance image-update %s --is-protected False" % image_id)  #older CEE version
                    logging.info("Deleting image %s" % image_id)
                    result = send_command(child, "glance image-delete %s" % image_id)
                    logging.info("Image delete output {}".format(result))
                else:
                    logging.info("Skipping image deletion of %s as it is only %s hours old"
                                 % (image_id, hours_old))

            storage_after = send_command(child, "df -h /dev/mapper/image-glance")
            logging.info("Storage after deleting the images {}".format(storage_after))
            logging.info("Completed deleting unused images ................!")
        else:
            logging.info("Skipping the cloud {}. Reason: password might have been expired or network issues".format(cloud))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', '-d', action='store_true', help='shows debug output')
    arguments = parser.parse_args()
    if arguments.debug:
        logging.basicConfig(format='[%(asctime)s] %(levelname)s: %(message)s', level=logging.DEBUG)
    else:
        logging.basicConfig(format='[%(asctime)s] %(levelname)s: %(message)s', level=logging.INFO)
    main()