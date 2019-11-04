#!/usr/bin/python

# (c) 2016, NetApp, Inc
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type


ANSIBLE_METADATA = {'metadata_version': '1.1',
                    'status': ['preview'],
                    'supported_by': 'community'}

DOCUMENTATION = """
---
module: na_santricity_firmware
version_added: "2.9"
short_description: NetApp E-Series manage firmware.
description:
    - Ensure specific firmware versions are activated on E-Series storage system.
author:
    - Nathan Swartz (@ndswartz)
extends_documentation_fragment:
    - netapp_eseries.santricity.santricity.santricity_doc
options:
    nvsram:
        description:
            - Path to the NVSRAM file.
        type: str
        required: false
    firmware:
        description:
            - Path to the firmware file.
        type: str
        required: false
    wait_for_completion:
        description:
            - This flag will cause module to wait for any upgrade actions to complete.
            - When changes are required to both firmware and nvsram and task is executed against SANtricity Web Services Proxy,
              the firmware will have to complete before nvsram can be installed.
        type: bool
        default: false
    ignore_mel_events:
        description:
            - This flag will force firmware to be activated in spite of the storage system mel event issues.
            - Use at your own risk.
        type: bool
        default: false
"""
EXAMPLES = """
- name: Ensure correct firmware versions
  na_santricity_firmware:
    ssid: "1"
    api_url: "https://192.168.1.100:8443/devmgr/v2"
    api_username: "admin"
    api_password: "adminpass"
    validate_certs: true
    nvsram: "path/to/nvsram"
    firmware: "path/to/bundle"
    wait_for_completion: true
    ignore_mel_events: true
- name: Ensure correct firmware versions
  na_santricity_firmware:
    ssid: "1"
    api_url: "https://192.168.1.100:8443/devmgr/v2"
    api_username: "admin"
    api_password: "adminpass"
    validate_certs: true
    nvsram: "path/to/nvsram"
    firmware: "path/to/firmware"
"""
RETURN = """
msg:
    description: Status and version of firmware and NVSRAM.
    type: str
    returned: always
    sample:
"""
import os
import multiprocessing

from time import sleep
from ansible.module_utils import six
from ansible_collections.netapp_eseries.santricity.plugins.module_utils.santricity import NetAppESeriesModule, create_multipart_formdata, request
from ansible.module_utils._text import to_native


class NetAppESeriesFirmware(NetAppESeriesModule):
    HEALTH_CHECK_TIMEOUT_MS = 120
    COMPATIBILITY_CHECK_TIMEOUT_SEC = 60
    DEFAULT_TIMEOUT = 60 * 15       # This will override the NetAppESeriesModule request method timeout.
    REBOOT_TIMEOUT_SEC = 15 * 60

    def __init__(self):
        ansible_options = dict(
            nvsram=dict(type="str", required=False),
            firmware=dict(type="str", required=False),
            wait_for_completion=dict(type="bool", default=False),
            ignore_mel_events=dict(type="bool", default=False))

        required_one_of = [["nvsram", "firmware"]]
        super(NetAppESeriesFirmware, self).__init__(ansible_options=ansible_options,
                                                    web_services_version="02.00.0000.0000",
                                                    required_one_of=required_one_of,
                                                    supports_check_mode=True)

        args = self.module.params
        self.nvsram = args["nvsram"]
        self.firmware = args["firmware"]
        self.wait_for_completion = args["wait_for_completion"]
        self.ignore_mel_events = args["ignore_mel_events"]

        self.nvsram_name = None
        self.firmware_name = None
        self.is_bundle_cache = None
        self.firmware_version_cache = None
        self.nvsram_version_cache = None
        self.upgrade_required = False
        self.firmware_upgrade_required = False
        self.nvsram_upgrade_required = False
        self.upgrade_in_progress = False
        self.module_info = dict()

        if self.nvsram:
            self.nvsram_name = os.path.basename(self.nvsram)
        if self.firmware:
            self.firmware_name = os.path.basename(self.firmware)

        self.start_mel_event = -1
        self.is_firmware_activation_started_mel_event_count = 1
        self.is_nvsram_download_completed_mel_event_count = 1
        self.proxy_wait_for_upgrade_mel_event_count = 1

    def is_upgrade_in_progress(self):
        """Determine whether an upgrade is already in progress."""
        in_progress = False

        if self.is_proxy():
            try:
                rc, status = self.request("storage-systems/%s/cfw-upgrade" % self.ssid)
                in_progress = status["running"]
            except Exception as error:
                self.module.fail_json(msg="Failed to retrieve upgrade status. Array [%s]. Error [%s]." % (self.ssid, error))
        else:
            in_progress = False

        return in_progress

    def is_firmware_bundled(self):
        """Determine whether supplied firmware is bundle."""
        if self.is_bundle_cache is None:
            with open(self.firmware, "rb") as fh:
                signature = fh.read(16).lower()

                if b"firmware" in signature:
                    self.is_bundle_cache = False
                elif b"combined_content" in signature:
                    self.is_bundle_cache = True
                else:
                    self.module.fail_json(msg="Firmware file is invalid. File [%s]. Array [%s]" % (self.firmware, self.ssid))

        return self.is_bundle_cache

    def firmware_version(self):
        """Retrieve firmware version of the firmware file. Return: bytes string"""
        if self.firmware_version_cache is None:

            # Search firmware file for bundle or firmware version
            with open(self.firmware, "rb") as fh:
                line = fh.readline()
                while line:
                    if self.is_firmware_bundled():
                        if b'displayableAttributeList=' in line:
                            for item in line[25:].split(b','):
                                key, value = item.split(b"|")
                                if key == b'VERSION':
                                    self.firmware_version_cache = value.strip(b"\n")
                            break
                    elif b"Version:" in line:
                        self.firmware_version_cache = line.split()[-1].strip(b"\n")
                        break
                    line = fh.readline()
                else:
                    self.module.fail_json(msg="Failed to determine firmware version. File [%s]. Array [%s]." % (self.firmware, self.ssid))
        return self.firmware_version_cache

    def nvsram_version(self):
        """Retrieve NVSRAM version of the NVSRAM file. Return: byte string"""
        if self.nvsram_version_cache is None:

            with open(self.nvsram, "rb") as fh:
                line = fh.readline()
                while line:
                    if b".NVSRAM Configuration Number" in line:
                        self.nvsram_version_cache = line.split(b'"')[-2]
                        break
                    line = fh.readline()
                else:
                    self.module.fail_json(msg="Failed to determine NVSRAM file version. File [%s]. Array [%s]." % (self.nvsram, self.ssid))
        return self.nvsram_version_cache

    def check_system_health(self):
        """Ensure E-Series storage system is healthy. Works for both embedded and proxy web services."""
        try:
            rc, response = self.request("storage-systems/%s/health-check" % self.ssid, method="POST")
            return response["successful"]
        except Exception as error:
            self.module.fail_json(msg="Health check failed! Array Id [%s]. Error[%s]." % (self.ssid, to_native(error)))

    def embedded_check_compatibility(self):
        """Verify files are compatible with E-Series storage system."""
        if self.nvsram:
            self.embedded_check_nvsram_compatibility()
        if self.firmware:
            self.embedded_check_bundle_compatibility()

    def embedded_check_nvsram_compatibility(self):
        """Verify the provided NVSRAM is compatible with E-Series storage system."""

        # Check nvsram compatibility
        try:
            files = [("nvsramimage", self.nvsram_name, self.nvsram)]
            headers, data = create_multipart_formdata(files=files)

            rc, compatible = self.request("firmware/embedded-firmware/%s/nvsram-compatibility-check" % self.ssid, method="POST", data=data, headers=headers)

            if not compatible["signatureTestingPassed"]:
                self.module.fail_json(msg="Invalid NVSRAM file. File [%s]." % self.nvsram)
            if not compatible["fileCompatible"]:
                self.module.fail_json(msg="Incompatible NVSRAM file. File [%s]." % self.nvsram)

            # Determine whether nvsram is required
            for module in compatible["versionContents"]:
                if module["bundledVersion"] != module["onboardVersion"]:
                    self.nvsram_upgrade_required = True

                # Update bundle info
                self.module_info.update({module["module"]: {"onboard_version": module["onboardVersion"], "bundled_version": module["bundledVersion"]}})

        except Exception as error:
            self.module.fail_json(msg="Failed to retrieve NVSRAM compatibility results. Array Id [%s]. Error[%s]." % (self.ssid, to_native(error)))

    def embedded_check_bundle_compatibility(self):
        """Verify the provided firmware bundle is compatible with E-Series storage system."""
        try:
            files = [("files[]", "blob", self.firmware)]
            headers, data = create_multipart_formdata(files=files, send_8kb=True)
            rc, compatible = self.request("firmware/embedded-firmware/%s/bundle-compatibility-check" % self.ssid, method="POST", data=data, headers=headers)

            # Determine whether valid and compatible firmware
            if not compatible["signatureTestingPassed"]:
                self.module.fail_json(msg="Invalid firmware bundle file. File [%s]." % self.firmware)
            if not compatible["fileCompatible"]:
                self.module.fail_json(msg="Incompatible firmware bundle file. File [%s]." % self.firmware)

            # Determine whether upgrade is required
            for module in compatible["versionContents"]:

                bundle_module_version = module["bundledVersion"].split(".")
                onboard_module_version = module["onboardVersion"].split(".")
                version_minimum_length = min(len(bundle_module_version), len(onboard_module_version))
                if bundle_module_version[:version_minimum_length] != onboard_module_version[:version_minimum_length]:
                    self.firmware_upgrade_required = True

                    # Check whether downgrade is being attempted
                    bundle_version = module["bundledVersion"].split(".")[:2]
                    onboard_version = module["onboardVersion"].split(".")[:2]
                    if bundle_version[0] < onboard_version[0] or (bundle_version[0] == onboard_version[0] and bundle_version[1] < onboard_version[1]):
                        self.module.fail_json(msg="Downgrades are not permitted. onboard [%s] > bundled[%s]."
                                                  % (module["onboardVersion"], module["bundledVersion"]))

                # Update bundle info
                self.module_info.update({module["module"]: {"onboard_version": module["onboardVersion"], "bundled_version": module["bundledVersion"]}})

        except Exception as error:
            self.module.fail_json(msg="Failed to retrieve bundle compatibility results. Array Id [%s]. Error[%s]." % (self.ssid, to_native(error)))

    def embedded_start_firmware_download(self):
        """Execute the firmware download."""
        headers, data = create_multipart_formdata(files=[("dlpfile", self.firmware_name, self.firmware)])
        try:
            rc, response = self.request("firmware/embedded-firmware?staged=false", method="POST", data=data, headers=headers, timeout=(30 * 60))
            self.upgrade_in_progress = True
        except Exception as error:
            self.module.fail_json(msg="Failed to upload and activate firmware. Array Id [%s]. Error[%s]." % (self.ssid, to_native(error)))

    def is_firmware_activation_started(self):
        """Determine if firmware activation has started."""
        try:
            rc, events = self.request("storage-systems/%s/mel-events?startSequenceNumber=%s&count=%s&cacheOnly=false&critical=false&includeDebug=false"
                                      % (self.ssid, self.start_mel_event, self.is_firmware_activation_started_mel_event_count), log_request=False)
            for event in events:
                controller_label = event["componentLocation"]["componentRelativeLocation"]["componentLabel"]
                self.start_mel_event = int(event["sequenceNumber"]) + 1

                if event["description"] == "Controller firmware download started":
                    self.module.log("(Controller %s) Controller firmware download started" % controller_label)
                elif event["description"] == "Controller firmware download completed":
                    self.module.log("(Controller %s) Controller firmware download completed" % controller_label)
                elif event["description"] == "Activate controller firmware started":
                    self.module.log("(Controller %s) Activate controller firmware started" % controller_label)
                    return True
                elif event["description"] == "Start-of-day routine begun":
                    self.module.log("(Controller %s) Start-of-day routine begun" % controller_label)
                elif event["description"] == "Controller reset":
                    self.module.log("(Controller %s) Controller reset" % controller_label)
                elif event["description"] == "Start-of-day routine completed":
                    self.module.log("(Controller %s) Start-of-day routine completed" % controller_label)

            if self.is_firmware_activation_started_mel_event_count == 1:
                self.is_firmware_activation_started_mel_event_count = 100
        except Exception as error:
            pass
        return False

    def embedded_start_nvsram_download(self):
        """Execute the nvsram download"""
        headers, data = create_multipart_formdata(files=[("nvsramfile", self.nvsram_name, self.nvsram)])
        try:
            rc, response = self.request("firmware/embedded-firmware/%s/nvsram" % self.ssid, method="POST", data=data, headers=headers, timeout=(15 * 60))
            self.upgrade_in_progress = True
        except Exception as error:
            self.module.fail_json(msg="Failed to upload and activate firmware. Array Id [%s]. Error[%s]." % (self.ssid, to_native(error)))

    def is_nvsram_download_completed(self):
        """Determine whether nvsram download has completed."""
        try:
            rc, events = self.request("storage-systems/%s/mel-events?startSequenceNumber=%s&count=%s&cacheOnly=false&critical=false&includeDebug=false"
                                      % (self.ssid, self.start_mel_event, self.is_nvsram_download_completed_mel_event_count), log_request=False)
            for event in events:
                controller_label = event["componentLocation"]["componentRelativeLocation"]["componentLabel"]
                self.start_mel_event = int(event["sequenceNumber"]) + 1

                if event["description"] == "Controller NVSRAM download completed":
                    self.module.log("(Controller %s) Controller NVSRAM download completed" % controller_label)
                    return True

            if self.is_nvsram_download_completed_mel_event_count == 1:
                self.is_nvsram_download_completed_mel_event_count = 100
        except Exception as error:
            pass
        return False

    def wait_for_web_services(self):
        """Wait for web services to report firmware and nvsram upgrade."""
        for count in range(int(self.REBOOT_TIMEOUT_SEC / 5)):
            try:
                rc, response = self.request("storage-systems/%s/graph/xpath-filter?query=/sa/saData" % self.ssid, log_request=False)
                bundle_display = [m["versionString"] for m in response[0]["extendedSAData"]["codeVersions"] if m["codeModule"] == "bundleDisplay"][0]

                if rc == 200 and six.b(bundle_display) == self.firmware_version() and six.b(response[0]["nvsramVersion"]) == self.nvsram_version():
                    self.upgrade_in_progress = False
                    break
            except Exception as error:
                pass
            sleep(5)
        else:
            self.module.fail_json(msg="Timeout waiting for Santricity Web Services. Array [%s]" % self.ssid)

    def embedded_upgrade(self):
        """Upload and activate both firmware and NVSRAM."""
        self.module.log("(embedded) firmware upgrade commencing...")
        if self.firmware:
            process = multiprocessing.Process(target=self.embedded_start_firmware_download)
            process.start()
            while process.is_alive():
                activation_started = self.is_firmware_activation_started()
                if not (self.wait_for_completion or self.nvsram_upgrade_required) and activation_started:
                    process.terminate()
                sleep(5)

        if self.nvsram:
            process = multiprocessing.Process(target=self.embedded_start_nvsram_download)
            process.start()
            while process.is_alive():
                download_completed = self.is_nvsram_download_completed()
                if not self.wait_for_completion and download_completed:
                    process.terminate()
                sleep(5)

        self.upgrade_in_progress = True
        if self.wait_for_completion:
            self.wait_for_web_services()

    def proxy_check_nvsram_compatibility(self, retries=10):
        """Verify nvsram is compatible with E-Series storage system."""
        self.module.log("Checking nvsram compatibility...")
        data = {"storageDeviceIds": [self.ssid]}
        try:
            rc, check = self.request("firmware/compatibility-check", method="POST", data=data)
            for count in range(int(self.COMPATIBILITY_CHECK_TIMEOUT_SEC / 5)):
                try:
                    rc, response = self.request("firmware/compatibility-check?requestId=%s" % check["requestId"])
                    if not response["checkRunning"]:
                        for result in response["results"][0]["nvsramFiles"]:
                            if result["filename"] == self.nvsram_name:
                                return
                        else:
                            self.module.fail_json(msg="NVSRAM is not compatible. NVSRAM [%s]. Array [%s]." % (self.nvsram_name, self.ssid))
                except Exception as error:
                    continue
                sleep(5)
            else:
                self.module.fail_json(msg="Failed to retrieve NVSRAM status update from proxy. Array [%s]." % self.ssid)

        except Exception as error:
            if retries:
                sleep(1)
                self.proxy_check_nvsram_compatibility(retries-1)
            else:
                self.module.fail_json(msg="Failed to receive NVSRAM compatibility information. Array [%s]. Error [%s]." % (self.ssid, to_native(error)))

    def proxy_check_firmware_compatibility(self, retries=10):
        """Verify firmware is compatible with E-Series storage system."""
        self.module.log("Checking firmware compatibility...")
        data = {"storageDeviceIds": [self.ssid]}
        try:
            rc, check = self.request("firmware/compatibility-check", method="POST", data=data)
            for count in range(int(self.COMPATIBILITY_CHECK_TIMEOUT_SEC / 5)):
                try:
                    rc, response = self.request("firmware/compatibility-check?requestId=%s" % check["requestId"])
                    if not response["checkRunning"]:
                        for result in response["results"][0]["cfwFiles"]:
                            if result["filename"] == self.firmware_name:
                                return
                        else:
                            self.module.fail_json(msg="Firmware bundle is not compatible. firmware [%s]. Array [%s]." % (self.firmware_name, self.ssid))
                    sleep(5)
                except Exception as error:
                    continue
            else:
                self.module.fail_json(msg="Failed to retrieve firmware status update from proxy. Array [%s]." % self.ssid)

        except Exception as error:
            if retries:
                sleep(1)
                self.proxy_check_firmware_compatibility(retries-1)
            else:
                self.module.fail_json(msg="Failed to receive firmware compatibility information. Array [%s]. Error [%s]." % (self.ssid, to_native(error)))

    def proxy_upload_and_check_compatibility(self):
        """Ensure firmware is uploaded and verify compatibility."""
        try:
            rc, cfw_files = self.request("firmware/cfw-files")

            if self.firmware:
                for file in cfw_files:
                    if file["filename"] == self.firmware_name:
                        break
                else:
                    fields = [("validate", "true")]
                    files = [("firmwareFile", self.firmware_name, self.firmware)]
                    headers, data = create_multipart_formdata(files=files, fields=fields)
                    try:
                        rc, response = self.request("firmware/upload", method="POST", data=data, headers=headers)
                    except Exception as error:
                        self.module.fail_json(msg="Failed to upload firmware bundle file. File [%s]. Array [%s]. Error [%s]."
                                                  % (self.firmware_name, self.ssid, to_native(error)))
                self.proxy_check_firmware_compatibility()

            if self.nvsram:
                for file in cfw_files:
                    if file["filename"] == self.nvsram_name:
                        break
                else:
                    fields = [("validate", "true")]
                    files = [("firmwareFile", self.nvsram_name, self.nvsram)]
                    headers, data = create_multipart_formdata(files=files, fields=fields)
                    try:
                        rc, response = self.request("firmware/upload", method="POST", data=data, headers=headers)
                    except Exception as error:
                        self.module.fail_json(msg="Failed to upload NVSRAM file. File [%s]. Array [%s]. Error [%s]."
                                                  % (self.nvsram_name, self.ssid, to_native(error)))
                self.proxy_check_nvsram_compatibility()
        except Exception as error:
            self.module.fail_json(msg="Failed to retrieve existing existing firmware files. Error [%s]" % to_native(error))

    def proxy_check_upgrade_required(self):
        """Staging is required to collect firmware information from the web services proxy."""
        # Verify controller consistency and get firmware versions
        if self.firmware:
            try:
                # Retrieve current bundle version
                if self.is_firmware_bundled():
                    rc, response = self.request("storage-systems/%s/graph/xpath-filter?query=/controller/codeVersions[codeModule='bundleDisplay']" % self.ssid)
                    current_firmware_version = six.b(response[0]["versionString"])
                else:
                    rc, response = self.request("storage-systems/%s/graph/xpath-filter?query=/sa/saData/fwVersion" % self.ssid)
                    current_firmware_version = six.b(response[0])

                # Determine whether upgrade is required
                if current_firmware_version != self.firmware_version():

                    current = current_firmware_version.split(b".")[:2]
                    upgrade = self.firmware_version().split(b".")[:2]
                    if current[0] < upgrade[0] or (current[0] == upgrade[0] and current[1] <= upgrade[1]):
                        self.firmware_upgrade_required = True
                    else:
                        self.module.fail_json(msg="Downgrades are not permitted. Firmware [%s]. Array [%s]." % (self.firmware, self.ssid))
            except Exception as error:
                self.module.fail_json(msg="Failed to retrieve controller firmware information. Array [%s]. Error [%s]" % (self.ssid, to_native(error)))

        # Determine current NVSRAM version and whether change is required
        if self.nvsram:
            try:
                rc, response = self.request("storage-systems/%s/graph/xpath-filter?query=/sa/saData/nvsramVersion" % self.ssid)
                if six.b(response[0]) != self.nvsram_version():
                    self.nvsram_upgrade_required = True

            except Exception as error:
                self.module.fail_json(msg="Failed to retrieve storage system's NVSRAM version. Array [%s]. Error [%s]" % (self.ssid, to_native(error)))

    def proxy_wait_for_upgrade(self):
        """Wait for SANtricity Web Services Proxy to report upgrade complete"""
        self.module.log("(Proxy) Waiting for upgrade to complete...")
        while True:
            try:
                rc, response = self.request("storage-systems/%s/cfw-upgrade" % self.ssid, log_request=False, ignore_errors=True)
                if not response["running"]:
                    if response["activationCompletionTime"]:
                        self.upgrade_in_progress = False
                        break

                    elif "errorMessage" in response:
                        self.module.fail_json(msg="Failed to complete upgrade. Array [%s]. Error [%s]." % (self.ssid, response["errorMessage"]))
                    else:
                        self.module.fail_json(msg="Failed to complete upgrade. Array [%s]." % self.ssid)

            except Exception as error:
                pass
                # self.module.fail_json(msg="Failed to get the upgrade status. Array [%s]. Error [%s]." % (self.ssid, to_native(error)))

            try:
                rc, events = self.request("storage-systems/%s/mel-events?startSequenceNumber=%s&count=%s&critical=false&includeDebug=false"
                                          % (self.ssid, self.start_mel_event, self.proxy_wait_for_upgrade_mel_event_count), log_request=False)

                for event in events:
                    controller_label = event["componentLocation"]["componentRelativeLocation"]["componentLabel"]
                    self.start_mel_event = int(event["sequenceNumber"]) + 1

                    if event["description"] == "Controller firmware download started":
                        self.module.log("(Controller %s) Controller firmware download started" % controller_label)
                    elif event["description"] == "Controller firmware download completed":
                        self.module.log("(Controller %s) Controller firmware download completed" % controller_label)
                    elif event["description"] == "Activate controller firmware started":
                        self.module.log("(Controller %s) Activate controller firmware started" % controller_label)
                    elif event["description"] == "Controller NVSRAM download completed":
                        self.module.log("(Controller %s) Controller NVSRAM download completed" % controller_label)
                    elif event["description"] == "Start-of-day routine begun":
                        self.module.log("(Controller %s) Start-of-day routine begun" % controller_label)
                    elif event["description"] == "Controller reset":
                        self.module.log("(Controller %s) Controller reset" % controller_label)
                    elif event["description"] == "Start-of-day routine completed":
                        self.module.log("(Controller %s) Start-of-day routine completed" % controller_label)

                if self.proxy_wait_for_upgrade_mel_event_count == 1:
                    self.proxy_wait_for_upgrade_mel_event_count = 100
            except Exception as error:
                pass

            sleep(5)

    def proxy_upgrade(self):
        """Activate previously uploaded firmware related files."""
        if self.firmware:
            self.module.log("(Proxy) Firmware upgrade commencing...")
            try:
                rc, response = self.request("storage-systems/%s/cfw-upgrade" % self.ssid, method="POST",
                                            data={"stageFirmware": False, "skipMelCheck": self.ignore_mel_events, "cfwFile": self.firmware_name})
            except Exception as error:
                self.module.fail_json(msg="Failed to initiate firmware upgrade. Array [%s]. Error [%s]." % (self.ssid, to_native(error)))

            self.upgrade_in_progress = True
            if self.wait_for_completion or self.nvsram_upgrade_required:
                self.proxy_wait_for_upgrade()

        if self.nvsram:
            self.module.log("(Proxy) NVSRAM upgrade commencing...")
            try:
                rc, response = self.request("storage-systems/%s/cfw-upgrade" % self.ssid, method="POST",
                                            data={"stageFirmware": False, "skipMelCheck": self.ignore_mel_events, "nvsramFile": self.nvsram_name})
            except Exception as error:
                self.module.fail_json(msg="Failed to initiate firmware upgrade. Array [%s]. Error [%s]." % (self.ssid, to_native(error)))

            self.upgrade_in_progress = True
            if self.wait_for_completion:
                self.proxy_wait_for_upgrade()

    def apply(self):
        """Upgrade controller firmware."""
        if self.is_upgrade_in_progress():
            self.module.fail_json(msg="Upgrade is already is progress. Array [%s]." % self.ssid)

        if self.is_embedded():
            self.embedded_check_compatibility()
        else:
            self.proxy_check_upgrade_required()

            # This will upload the firmware files to the web services proxy but not to the controller
            if self.firmware_upgrade_required or self.nvsram_upgrade_required:
                self.proxy_upload_and_check_compatibility()

        # Perform upgrade
        if (self.firmware_upgrade_required or self.nvsram_upgrade_required) and not self.module.check_mode:
            if self.is_embedded():
                self.embedded_upgrade()
            else:
                self.proxy_upgrade()

        self.module.exit_json(changed=(self.firmware_upgrade_required or self.nvsram_upgrade_required), upgrade_in_process=self.upgrade_in_progress)


if __name__ == '__main__':
    firmware = NetAppESeriesFirmware()
    firmware.apply()