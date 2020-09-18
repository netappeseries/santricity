=======================================
Netapp_Eseries.Santricity Release Notes
=======================================

.. contents:: Topics


v1.1.0
======

Release Summary
---------------

This release focused on providing volume details to through the netapp_volumes_by_initiators in the na_santricity_facts module, improving on the nar_santricity_common role storage system API information and resolving issues.

Minor Changes
-------------

- Add functionality to remove all inventory configuration in the nar_santricity_host role. Set configuration.eseries_remove_all_configuration=True to remove all storage pool/volume configuration, host, hostgroup, and lun mapping configuration.
- Add host_types, host_port_protocols, host_port_information, hostside_io_interface_protocols to netapp_volumes_by_initiators in the na_santricity_facts module.
- Add storage pool information to the volume_by_initiator facts.
- Add storage system not found exception to the common role's build_info task.
- Add volume_metadata option to na_santricity_volume module, add volume_metadata information to the netapp_volumes_by_initiators dictionary in na_santricity_facts module, and update the nar_santricity_host role with the option.
- Improve nar_santricity_common storage system api determinations; attempts to discover the storage system using the information provided in the inventory before attempting to search the subnet.
- Increased the storage system discovery connection timeouts to 30 seconds to prevent systems from not being discovered over slow connections.
- Minimize the facts gathered for the host initiators.
- Update ib iser determination to account for changes in firmware 11.60.2.
- Use existing Web Services Proxy storage system identifier when one is already created and one is not provided in the inventory.
- Utilize eseries_iscsi_iqn before searching host for iqn in nar_santricity_host role.

Bugfixes
--------

- Fix check_port_type method for ib iser when ib is the port type.
- Fix examples in the netapp_e_mgmt_interface module.
- Fix issue with changing host port name.
- Fix na_santricity_lun_mapping unmapping issue; previously mapped volumes failed to be unmapped.
