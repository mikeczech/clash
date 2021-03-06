""" Clash """

import logging
from typing import List, Dict, Optional, Any
import uuid
import copy
import json
import time

import os
import os.path

import jinja2
import googleapiclient.discovery
from google.cloud import pubsub_v1 as pubsub
from google.cloud.pubsub_v1.types import MessageStoragePolicy
from google.cloud import logging as glogging

logger = logging.getLogger(__name__)

DEFAULT_JOB_CONFIG = {
    "project_id": "my-gcp-project",
    "image": "google/cloud-sdk",
    "privileged": False,
    "preemptible": False,
    "zone": "europe-west1-b",
    "region": "europe-west1",
    "subnetwork": "default-europe-west1",
    "machine_type": "n1-standard-4",
    "service_account": "default",
    "disk_image": {"project": "gce-uefi-images", "family": "cos-stable"},
    "scopes": [
        "https://www.googleapis.com/auth/bigquery",
        "https://www.googleapis.com/auth/compute",
        "https://www.googleapis.com/auth/devstorage.read_write",
        "https://www.googleapis.com/auth/devstorage.full_control",
        "https://www.googleapis.com/auth/logging.write",
        "https://www.googleapis.com/auth/monitoring",
        "https://www.googleapis.com/auth/pubsub",
        "https://www.googleapis.com/auth/cloud-platform",
        "https://www.googleapis.com/auth/cloudplatformprojects",
    ],
    "allowed_persistence_regions": [
        "europe-north1",
        "europe-west1",
        "europe-west3",
        "europe-west4",
    ],
}


class JobConfigBuilder:
    """ Builds configurations for jobs """

    def __init__(self, base_config: Optional[Dict[str, Any]] = None):
        self.config = copy.deepcopy(base_config or DEFAULT_JOB_CONFIG)

    def project_id(self, project_id):
        self.config["project_id"] = project_id
        return self

    def image(self, image):
        self.config["image"] = image
        return self

    def privileged(self, privileged):
        self.config["privileged"] = privileged
        return self

    def preemptible(self, preemptible):
        self.config["preemptible"] = preemptible
        return self

    def zone(self, zone):
        self.config["zone"] = zone
        return self

    def region(self, region):
        self.config["region"] = region
        return self

    def subnetwork(self, subnetwork):
        self.config["subnetwork"] = subnetwork
        return self

    def machine_type(self, machine_type):
        self.config["machine_type"] = machine_type
        return self

    def service_account(self, service_account):
        self.config["service_account"] = service_account
        return self

    def disk_image(self, disk_image):
        self.config["disk_image"] = disk_image
        return self

    def scopes(self, scopes):
        self.config["scopes"] = scopes
        return self

    def labels(self, labels):
        self.config["labels"] = labels
        return self

    def build(self):
        return copy.deepcopy(self.config)


class MemoryCache:
    """ Having this class avoids dependency issues with the compute engine client"""

    _CACHE = {}

    def get(self, url):
        return MemoryCache._CACHE.get(url)

    def set(self, url, content):
        MemoryCache._CACHE[url] = content


class CloudSdk:
    """ Provides access to the GCP services (e.g. logging, compute engine, etc.) """

    def __init__(self):
        pass

    def get_compute_client(self):
        return googleapiclient.discovery.build("compute", "v1", cache=MemoryCache())

    def get_publisher(self):
        return pubsub.PublisherClient()

    def get_subscriber(self):
        return pubsub.SubscriberClient()

    def get_logging(self, project=None):
        if project:
            return glogging.Client(project=project)
        return glogging.Client()


class CloudInitConfig:
    """
    This class provides means to create a configuration for cloud-init.

    (e.g. one which starts a Docker container on the target machine)
    """

    def __init__(
        self,
        vm_name,
        script,
        job_config,
        env_vars: Optional[Dict[str, str]] = None,
        gcs_target: Optional[Dict[str, str]] = None,
        gcs_mounts: Optional[Dict[str, str]] = None,
    ):
        self.template_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(
                searchpath=os.path.join(os.path.dirname(__file__), "templates")
            )
        )
        self.vm_name = vm_name
        self.script = script
        self.job_config = job_config
        self.env_vars = env_vars or {}
        self.gcs_target = gcs_target or {}
        self.gcs_mounts = gcs_mounts or {}

    def render(self):
        """
        Renders the cloud-init configuration

        Returns:
            string: a cloud-init configuration file
        """
        clash_runner_script = self.template_env.get_template(
            "clash_runner.sh.j2"
        ).render(
            vm_name=self.vm_name,
            zone=self.job_config["zone"],
            image=self.job_config["image"],
            privileged=self.job_config["privileged"],
        )

        env_var_file = "\n".join(
            [f"{var}={value}" for var, value in self.env_vars.items()]
        )

        return self.template_env.get_template("cloud-init.yaml.j2").render(
            vm_name=self.vm_name,
            clash_runner_script=clash_runner_script,
            gcs_target=self.gcs_target,
            gcs_mounts=self.gcs_mounts,
            script=self.script,
            env_var_file=env_var_file,
        )


class MachineConfig:
    """
    This class provides methods for creating a machine configuration
    for the Google Compute Engine.
    """

    def __init__(self, compute, vm_name, cloud_init, job_config):
        self.template_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(
                searchpath=os.path.join(os.path.dirname(__file__), "templates")
            )
        )
        self.compute = compute
        self.vm_name = vm_name
        self.cloud_init = cloud_init
        self.job_config = job_config

    def to_dict(self):
        """
        Creates the machine configuration

        Returns:
            dict: the configuration
        """
        image_response = (
            self.compute.images()
            .getFromFamily(
                project=self.job_config["disk_image"]["project"],
                family=self.job_config["disk_image"]["family"],
            )
            .execute()
        )
        source_disk_image = image_response["selfLink"]

        rendered = json.loads(
            self.template_env.get_template("machine_config.json.j2").render(
                vm_name=self.vm_name,
                source_image=source_disk_image,
                project_id=self.job_config["project_id"],
                machine_type=self.job_config["machine_type"],
                region=self.job_config["region"],
                scopes=self.job_config["scopes"],
                subnetwork=self.job_config["subnetwork"],
                preemptible=self.job_config["preemptible"],
                service_account=self.job_config["service_account"],
                labels=self.job_config.get("labels", {}),
            )
        )

        rendered["metadata"]["items"][0]["value"] = self.cloud_init.render()

        return rendered


class JobRuntimeSpec:
    """ Specifies runtime properties of jobs """

    def __init__(
        self,
        args: List[str],
        env_vars: Optional[Dict[str, str]] = None,
        gcs_mounts: Optional[Dict[str, str]] = None,
        gcs_target: Optional[Dict[str, str]] = None,
    ):
        self.args = args
        self.env_vars = env_vars or {}
        self.gcs_mounts = gcs_mounts or {}
        self.gcs_target = gcs_target or {}


class JobFactory:
    def __init__(self, job_config, gcloud=CloudSdk()):
        self.job_config = job_config
        self.gcloud = gcloud

    def create(self, name_prefix):
        return Job(
            name_prefix=name_prefix, job_config=self.job_config, gcloud=self.gcloud
        )


class JobGroup:
    """
    This class allows the creation of multiple jobs.
    """

    def __init__(self, name, job_factory):
        """
        Constructs a new group.

        :param name the name of the group
        :param job_factory a factory that creates individual jobs
        """
        self.name = name
        self.job_factory = job_factory
        self.job_config = job_factory.job_config
        self.gcloud = job_factory.gcloud

        self.job_specs = []
        self.running_jobs = []
        self.jobs_status_codes = []

    def add_job(self, runtime_spec):
        """
        Adds a job to the group.
        :param runtime_spec runtime specification of the job
        """
        self.job_specs.append(runtime_spec)

    def run(self):
        """
        Runs all jobs that are part of the group.
        """
        for spec_id, spec in enumerate(self.job_specs):
            job = self.job_factory.create(name_prefix=f"{self.name}-{spec_id}")
            job.run(
                args=spec.args,
                env_vars=spec.env_vars,
                gcs_mounts=spec.gcs_mounts,
                gcs_target=spec.gcs_target,
            )
            # arrays are thread-safe in Python (due to GIL)
            job.on_finish(self.jobs_status_codes.append)
            self.running_jobs.append(job)

    def wait(self):
        """
        Blocks until all jobs of the group are complete.

        :returns true if all jobs succeeded else false
        """
        while len(self.jobs_status_codes) != len(self.running_jobs):
            time.sleep(1)

        return all(map(lambda code: code == 0, self.jobs_status_codes))

    def clean_up(self):
        """
        Manual clean up. This method is a workaround and will disappear soon.
        """
        for job in self.running_jobs:
            job.clean_up()

    def is_group(self):
        return True

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.clean_up()


def translate_args_to_script(args: List[str]):
    res = []
    for arg in args:
        if " " in arg:
            res.append(f"'{arg}'")
        else:
            res.append(arg)
    return " ".join(res)


class Job:
    """
    This class creates Clash-jobs and runs them on the Google Compute Engine (GCE).
    """

    POLLING_INTERVAL_SECONDS = 30

    def __init__(
        self,
        job_config,
        name=None,
        name_prefix=None,
        gcloud: Optional[CloudSdk] = None,
        timeout_seconds: Optional[int] = None,
    ):
        self.gcloud = gcloud or CloudSdk()
        self.job_config = job_config
        self.started = False

        self.job_status_topic = None
        self.job_status_subscription = None
        self.timeout_seconds = timeout_seconds

        if not name:
            self.name = "clash-job-{}".format(str(uuid.uuid1())[0:16])
            if name_prefix:
                self.name = f"{name_prefix}-" + self.name
        else:
            self.name = name

    def _wait_for_operation(self, operation, is_global_op):
        """ Waits for an GCE operation to finish """
        compute = self.gcloud.get_compute_client()
        operations_client = (
            compute.globalOperations() if is_global_op else compute.zoneOperations()
        )

        args = {"project": self.job_config["project_id"], "operation": operation}
        if not is_global_op:
            args["zone"] = self.job_config["zone"]

        while True:
            result = operations_client.get(**args).execute()

            if result["status"] == "DONE":
                if "error" in result:
                    raise Exception(result["error"])
                return result

            time.sleep(1)

    def _create_instance_template(self, machine_config):
        """ Creates a GCE Instance Template and waits for it """
        template_op = (
            self.gcloud.get_compute_client()
            .instanceTemplates()
            .insert(
                project=self.job_config["project_id"],
                body={"name": self.name, "properties": machine_config},
            )
            .execute()
        )
        self._wait_for_operation(template_op["name"], True)

    def _create_managed_instance_group(self, size):
        """ Create GCE Instance Group and waits for it """
        template_op = (
            self.gcloud.get_compute_client()
            .instanceGroupManagers()
            .insert(
                project=self.job_config["project_id"],
                zone=self.job_config["zone"],
                body={
                    "baseInstanceName": self.name,
                    "instanceTemplate": f"global/instanceTemplates/{self.name}",
                    "name": self.name,
                    "targetSize": size,
                },
            )
            .execute()
        )
        self._wait_for_operation(template_op["name"], False)

    def run(
        self,
        args: List[str],
        env_vars: Optional[Dict[str, str]] = None,
        gcs_target: Optional[Dict[str, str]] = None,
        gcs_mounts: Optional[Dict[str, str]] = None,
        wait_for_result: bool = False,
    ):
        """
        Runs a script which is given as a string.

        Args:
            script (string): A Bash script which will be executed on GCE.
            env_vars (dict): Environment variables which can be used by the script.
            gcs_target (dict): Files which will be copied to GCS when the script is done.
            gcs_mounts (dict): Buckets which will be mounted using gcsfuse (if available).
            wait_for_result (bool): If true, blocks until the job is complete.
        """
        subscriber = self.gcloud.get_subscriber()
        publisher = self.gcloud.get_publisher()
        env_vars = env_vars or {}
        gcs_target = gcs_target or {}
        gcs_mounts = gcs_mounts or {}
        script = translate_args_to_script(args)

        machine_config = self._create_machine_config(
            script, env_vars, gcs_target, gcs_mounts
        )

        self.job_status_topic = None
        self.job_status_subscription = None
        try:
            self.job_status_topic = self._create_status_topic(publisher)
            self.job_status_subscription = self._create_status_subscription(
                publisher, subscriber
            )
            self._create_instance_template(machine_config)
            self._create_managed_instance_group(1)
            self.started = True
            if wait_for_result:
                return self.attach(self.timeout_seconds)
        except Exception as ex:
            if self.started:
                try:
                    self._remove_instance_group()
                except Exception as e:
                    logger.warning(
                        f"Could not remove instance group (not running?). Message: {e}"
                    )

            if self.job_status_topic:
                try:
                    publisher.delete_topic(self.job_status_topic)
                except Exception as e:
                    logger.warning(f"Could not remove pubsub topic. Message: {e}")

            if self.job_status_subscription:
                try:
                    subscriber.delete_subscription(self.job_status_subscription)
                except Exception as e:
                    logger.warning(
                        f"Could not remove pubsub subscription. Message: {e}"
                    )

            raise ex

    def run_file(
        self,
        script_file,
        env_vars: Optional[Dict[str, str]] = None,
        gcs_target: Optional[Dict[str, str]] = None,
        gcs_mounts: Optional[Dict[str, str]] = None,
        wait_for_result: bool = False,
    ):
        """
        Runs a script which is given as a file.

        Args:
            script_file (string): Path to a Bash script which will be executed on GCE.
            env_vars (dict): Environment variables which can be used by the script.
            gcs_target (dict): Files which will be copied to GCS when the script is done.
            gcs_mounts (dict): Buckets which will be mounted using gcsfuse (if available).
        """
        env_vars = env_vars or {}
        gcs_target = gcs_target or {}
        gcs_mounts = gcs_mounts or {}

        with open(script_file, "r") as f:
            script = f.read()
        return self.run(script, wait_for_result, env_vars, gcs_target)

    def _create_machine_config(self, script, env_vars, gcs_target, gcs_mounts):
        cloud_init = CloudInitConfig(
            self.name, script, self.job_config, env_vars, gcs_target, gcs_mounts
        )

        return MachineConfig(
            self.gcloud.get_compute_client(), self.name, cloud_init, self.job_config
        ).to_dict()

    def on_finish(self, callback):
        """
        Sets a callback function which is executed when the job is complete.
        """
        if not self.started:
            raise ValueError("The job is not running")

        def pubsub_callback(message):
            data = json.loads(message.data)
            callback(data["status"])
            message.ack()

        self.gcloud.get_subscriber().subscribe(
            self.job_status_subscription, pubsub_callback
        )

    def _retrieve_active_instance_groups(self) -> List[str]:
        res = []
        for group in (
            self.gcloud.get_compute_client()
            .instanceGroups()
            .list(project=self.job_config["project_id"], zone=self.job_config["zone"])
            .execute()["items"]
        ):
            res.append(group["name"])
        return res

    def _wait_for_instance_group_removal(self) -> None:
        while True:
            active_instance_groups = self._retrieve_active_instance_groups()
            if self.name in active_instance_groups:
                logger.debug("Instance group is still active. Waiting...")
                time.sleep(Job.POLLING_INTERVAL_SECONDS)
            else:
                break

    def clean_up(self):
        """
        Deletes resources which are left-overs after a job is complete.

        e.g. instances templates which cannot be deleted before the
        related instance group is not present anymore.
        """
        if self.started:
            logger.debug("Deleting instance template...")
            self._wait_for_instance_group_removal()
            self._remove_instance_template()

    def _remove_instance_template(self):
        if not self.started:
            raise Exception("Job is not running")
        template_op = (
            self.gcloud.get_compute_client()
            .instanceTemplates()
            .delete(project=self.job_config["project_id"], instanceTemplate=self.name)
            .execute()
        )
        self._wait_for_operation(template_op["name"], True)
        logger.debug("Successfully removed instance template.")

    def _remove_instance_group(self):
        if not self.started:
            raise Exception("Job is not running")
        template_op = (
            self.gcloud.get_compute_client()
            .instanceGroupManagers()
            .delete(
                project=self.job_config["project_id"],
                zone=self.job_config["zone"],
                instanceGroupManager=self.name,
            )
            .execute()
        )
        self._wait_for_operation(template_op["name"], False)
        logger.debug("Successfully removed managed instance group.")

    def attach(self, timeout_seconds: Optional[int] = None) -> Optional[Dict[str, str]]:
        """
        Blocks until the job terminates.
        """
        if not self.started:
            raise ValueError("The job is not running")

        subscriber = self.gcloud.get_subscriber()
        start_time = time.time()
        while not timeout_seconds or (time.time() - start_time) <= timeout_seconds:
            message = self._pull_message(subscriber, self.job_status_subscription)
            if message:
                return json.loads(message.data)

        raise TimeoutError(f"The job took longer than {timeout_seconds} seconds")

    def _pull_message(self, subscriber, subscription_path):
        """ Pulls a PubSub message """
        response = subscriber.pull(
            subscription_path,
            max_messages=1,
            return_immediately=False,
            timeout=Job.POLLING_INTERVAL_SECONDS,
        )

        if len(response.received_messages) > 0:
            message = response.received_messages[0]
            ack_id = message.ack_id
            subscriber.acknowledge(subscription_path, [ack_id])
            return message.message

        return None

    def _create_status_topic(self, publisher):
        """ Creates a PubSub topic for the status """
        job_status_topic = publisher.topic_path(
            self.job_config["project_id"], self.name
        )

        message_storage_policy = (
            lambda regions: MessageStoragePolicy(allowed_persistence_regions=regions)
            if regions
            else None
        )
        publisher.create_topic(
            job_status_topic,
            message_storage_policy=message_storage_policy(
                self.job_config.get("allowed_persistence_regions")
            ),
        )
        return job_status_topic

    def _create_status_subscription(self, publisher, subscriber):
        """ Creates a PubSub subscription for a job's status """
        project_id = self.job_config["project_id"]
        topics = [path.name for path in publisher.list_topics(f"projects/{project_id}")]

        if self.job_status_topic not in topics:
            raise ValueError(f"Could not find status topic for job {self.name}")

        subscription_path = subscriber.subscription_path(project_id, self.name)
        subscriber.create_subscription(subscription_path, self.job_status_topic)

        return subscription_path

    def is_group(self):
        return False

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.clean_up()
