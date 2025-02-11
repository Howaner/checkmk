#!/usr/bin/python
# -*- encoding: utf-8; py-indent-offset: 4 -*-
# +------------------------------------------------------------------+
# |             ____ _               _        __  __ _  __           |
# |            / ___| |__   ___  ___| | __   |  \/  | |/ /           |
# |           | |   | '_ \ / _ \/ __| |/ /   | |\/| | ' /            |
# |           | |___| | | |  __/ (__|   <    | |  | | . \            |
# |            \____|_| |_|\___|\___|_|\_\___|_|  |_|_|\_\           |
# |                                                                  |
# | Copyright Mathias Kettner 2014             mk@mathias-kettner.de |
# +------------------------------------------------------------------+
#
# This file is part of Check_MK.
# The official homepage is at http://mathias-kettner.de/check_mk.
#
# check_mk is free software;  you can redistribute it and/or modify it
# under the  terms of the  GNU General Public License  as published by
# the Free Software Foundation in version 2.  check_mk is  distributed
# in the hope that it will be useful, but WITHOUT ANY WARRANTY;  with-
# out even the implied warranty of  MERCHANTABILITY  or  FITNESS FOR A
# PARTICULAR PURPOSE. See the  GNU General Public License for more de-
# tails. You should have  received  a copy of the  GNU  General Public
# License along with GNU Make; see the file  COPYING.  If  not,  write
# to the Free Software Foundation, Inc., 51 Franklin St,  Fifth Floor,
# Boston, MA 02110-1301 USA.

import os
import time
import re
import shutil
import cStringIO
from typing import Dict  # pylint: disable=unused-import

import cmk
import cmk.utils.store as store

import cmk.gui.config as config
import cmk.gui.userdb as userdb
import cmk.gui.hooks as hooks
from cmk.gui.i18n import _
from cmk.gui.exceptions import (
    MKGeneralException,
    MKAuthException,
    MKUserError,
)
from cmk.gui.htmllib import HTML
from cmk.gui.globals import html, current_app

from cmk.gui.watolib.utils import (
    wato_root_dir,
    rename_host_in_list,
    convert_cgroups_from_tuple,
    host_attribute_matches,
    default_site,
    format_config_value,
    ALL_HOSTS,
    ALL_SERVICES,
)
from cmk.gui.watolib.changes import add_change
from cmk.gui.watolib.automations import check_mk_automation
from cmk.gui.watolib.sidebar_reload import need_sidebar_reload
from cmk.gui.watolib.host_attributes import host_attribute_registry

from cmk.gui.plugins.watolib.utils import wato_fileheader

if cmk.is_managed_edition():
    import cmk.gui.cme.managed as managed

# Names:
# folder_path: Path of the folders directory relative to etc/check_mk/conf.d/wato
#              The root folder is "". No trailing / is allowed here.
# wato_info:   The dictionary that is saved in the folder's .wato file

# Terms:
# create, delete   mean actual filesystem operations
# add, remove      mean just modifications in the data structures


class WithPermissions(object):
    def may(self, how):  # how is "read" or "write"
        try:
            self._user_needs_permission(how)
            return True
        except MKAuthException:
            return False

    def reason_why_may_not(self, how):
        try:
            self._user_needs_permission(how)
            return False
        except MKAuthException as e:
            return HTML("%s" % e)

    def need_permission(self, how):
        self._user_needs_permission(how)

    def _user_needs_permission(self, how):
        raise NotImplementedError()


class WithPermissionsAndAttributes(WithPermissions):
    """Base class containing a couple of generic permission checking functions, used for Host and Folder"""

    def __init__(self):
        super(WithPermissionsAndAttributes, self).__init__()
        self._attributes = {}
        self._effective_attributes = None

    # .--------------------------------------------------------------------.
    # | ATTRIBUTES                                                         |
    # '--------------------------------------------------------------------'

    def attributes(self):
        return self._attributes

    def attribute(self, attrname, default_value=None):
        return self.attributes().get(attrname, default_value)

    def set_attribute(self, attrname, value):
        self._attributes[attrname] = value

    def has_explicit_attribute(self, attrname):
        return attrname in self.attributes()

    def effective_attributes(self):
        raise NotImplementedError()

    def effective_attribute(self, attrname, default_value=None):
        return self.effective_attributes().get(attrname, default_value)

    def remove_attribute(self, attrname):
        del self.attributes()[attrname]

    def drop_caches(self):
        self._effective_attributes = None

    def _cache_effective_attributes(self, effective):
        self._effective_attributes = effective.copy()

    def _get_cached_effective_attributes(self):
        if self._effective_attributes is None:
            raise KeyError("Not cached")
        else:
            return self._effective_attributes.copy()


class BaseFolder(WithPermissionsAndAttributes):
    """Base class of SearchFolder and Folder. Implements common methods"""

    def hosts(self):
        raise NotImplementedError()

    def host_names(self):
        return self.hosts().keys()

    def host(self, host_name):
        return self.hosts().get(host_name)

    def has_host(self, host_name):
        return host_name in self.hosts()

    def has_hosts(self):
        return len(self.hosts()) != 0

    def host_validation_errors(self):
        return validate_all_hosts(self.host_names())

    def is_disk_folder(self):
        return False

    def is_search_folder(self):
        return False

    def has_parent(self):
        return self.parent() is not None

    def parent(self):
        raise NotImplementedError()

    def is_same_as(self, folder):
        return self == folder or self.path() == folder.path()

    def path(self):
        raise NotImplementedError()

    def is_current_folder(self):
        return self.is_same_as(Folder.current())

    def is_parent_of(self, maybe_child):
        return maybe_child.parent() == self

    def is_transitive_parent_of(self, maybe_child):
        if self.is_same_as(maybe_child):
            return True
        elif maybe_child.has_parent():
            return self.is_transitive_parent_of(maybe_child.parent())
        return False

    def is_root(self):
        return not self.has_parent()

    def parent_folder_chain(self):
        folders = []
        folder = self.parent()
        while folder:
            folders.append(folder)
            folder = folder.parent()
        return folders[::-1]

    def show_breadcrump(self, link_to_folder=False, keepvarnames=None):
        if keepvarnames is True:
            uri_func = html.makeuri
            keepvars = []
        else:
            uri_func = html.makeuri_contextless

            if keepvarnames is None:
                keepvarnames = ["mode"]

            keepvars = [(name, html.request.var(name)) for name in keepvarnames]
            if link_to_folder:
                keepvars.append(("mode", "folder"))

        def render_component(folder):
            return '<a href="%s">%s</a>' % \
                (uri_func([ ("folder", folder.path())] + keepvars),
                 html.attrencode(folder.title()))

        def breadcrump_element_start(end='', z_index=0):
            html.open_li(style="z-index:%d;" % z_index)
            html.div('', class_=["left", end])

        def breadcrump_element_end(end=''):
            html.div('', class_=["right", end])
            html.close_li()

        parts = []
        for folder in self.parent_folder_chain():
            parts.append(render_component(folder))

        # The current folder (with link or without link)
        if link_to_folder:
            parts.append(render_component(self))
        else:
            parts.append(html.attrencode(self.title()))

        # Render the folder path
        html.open_div(class_=["folderpath"])
        html.open_ul()
        num = 0
        for part in parts:
            if num == 0:
                breadcrump_element_start('end', z_index=100 + num)
            else:
                breadcrump_element_start(z_index=100 + num)
            html.open_div(class_=["content"])
            html.write(part)
            html.close_div()

            breadcrump_element_end(num == len(parts) - 1 and
                                   not (self.has_subfolders() and not link_to_folder) and "end" or
                                   "")
            num += 1

        # Render the current folder when having subfolders
        if not link_to_folder and self.has_subfolders() and self.visible_subfolders():
            breadcrump_element_start(z_index=100 + num)
            html.open_div(class_=["content"])
            html.open_form(name="folderpath", method="GET")
            html.dropdown(
                "folder", [("", "")] + self.subfolder_choices(),
                class_="folderpath",
                onchange="folderpath.submit();")
            if keepvarnames is True:
                html.hidden_fields()
            else:
                for var in keepvarnames:
                    html.hidden_field(var, html.request.var(var))
            html.close_form()
            html.close_div()
            breadcrump_element_end('end')

        html.close_ul()
        html.close_div()

    def name(self):
        raise NotImplementedError()

    def title(self):
        raise NotImplementedError()

    def visible_subfolders(self):
        raise NotImplementedError()

    def subfolder(self, name):
        raise NotImplementedError()

    def has_subfolders(self):
        raise NotImplementedError()

    def subfolder_choices(self):
        raise NotImplementedError()

    def move_subfolder_to(self, subfolder, target_folder):
        raise NotImplementedError()

    def create_subfolder(self, name, title, attributes):
        raise NotImplementedError()

    def edit_url(self, backfolder=None):
        raise NotImplementedError()

    def edit(self, new_title, new_attributes):
        raise NotImplementedError()

    def locked(self):
        raise NotImplementedError()

    def create_hosts(self, entries):
        raise NotImplementedError()

    def site_id(self):
        raise NotImplementedError()


class CREFolder(BaseFolder):
    """This class represents a WATO folder that contains other folders and hosts."""
    # .--------------------------------------------------------------------.
    # | STATIC METHODS                                                     |
    # '--------------------------------------------------------------------'

    @staticmethod
    def all_folders():
        if "wato_folders" not in current_app.g:
            wato_folders = current_app.g["wato_folders"] = {}
            Folder("", "").add_to_dictionary(wato_folders)
        return current_app.g["wato_folders"]

    @staticmethod
    def folder_choices():
        return Folder.root_folder().recursive_subfolder_choices()

    @staticmethod
    def folder_choices_fulltitle():
        return Folder.root_folder().recursive_subfolder_choices(current_depth=0, pretty=False)

    @staticmethod
    def folder(folder_path):
        if folder_path in Folder.all_folders():
            return Folder.all_folders()[folder_path]
        else:
            raise MKGeneralException("No WATO folder %s." % folder_path)

    @staticmethod
    def create_missing_folders(folder_path):
        folder = Folder.folder("")
        for subfolder_name in Folder._split_folder_path(folder_path):
            if folder.has_subfolder(subfolder_name):
                folder = folder.subfolder(subfolder_name)
            else:
                folder = folder.create_subfolder(subfolder_name, subfolder_name, {})

    @staticmethod
    def _split_folder_path(folder_path):
        if not folder_path:
            return []
        return folder_path.split("/")

    @staticmethod
    def folder_exists(folder_path):
        return os.path.exists(wato_root_dir() + folder_path)

    @staticmethod
    def root_folder():
        return Folder.folder("")

    @staticmethod
    def invalidate_caches():
        try:
            del current_app.g["wato_folders"]
        except KeyError:
            pass
        Folder.root_folder().drop_caches()

    # Find folder that is specified by the current URL. This is either by a folder
    # path in the variable "folder" or by a host name in the variable "host". In the
    # latter case we need to load all hosts in all folders and actively search the host.
    # Another case is the host search which has the "host_search" variable set. To handle
    # the later case we call .current() of SearchFolder() to let it decide whether or not
    # this is a host search. This method has to return a folder in all cases.
    @staticmethod
    def current():
        if "wato_current_folder" in current_app.g:
            return current_app.g["wato_current_folder"]

        folder = SearchFolder.current_search_folder()
        if folder:
            return folder

        if html.request.has_var("folder"):
            try:
                folder = Folder.folder(html.request.var("folder"))
            except MKGeneralException as e:
                raise MKUserError("folder", "%s" % e)
        else:
            host_name = html.request.var("host")
            folder = Folder.root_folder()
            if host_name:  # find host with full scan. Expensive operation
                host = Host.host(host_name)
                if host:
                    folder = host.folder()

        Folder.set_current(folder)
        return folder

    @staticmethod
    def current_disk_folder():
        folder = Folder.current()
        while not folder.is_disk_folder():
            folder = folder.parent()
        return folder

    @staticmethod
    def set_current(folder):
        current_app.g["wato_current_folder"] = folder

    # .-----------------------------------------------------------------------.
    # | CONSTRUCTION, LOADING & SAVING                                        |
    # '-----------------------------------------------------------------------'

    def __init__(self,
                 name,
                 folder_path=None,
                 parent_folder=None,
                 title=None,
                 attributes=None,
                 root_dir=None):
        super(CREFolder, self).__init__()
        self._name = name
        self._parent = parent_folder
        self._subfolders = {}

        self._choices_for_moving_host = None

        self._root_dir = root_dir
        if self._root_dir:
            self._root_dir = root_dir.rstrip("/") + "/"  # FIXME: ugly
        else:
            self._root_dir = wato_root_dir()

        if folder_path is not None:
            self._init_by_loading_existing_directory(folder_path)
        else:
            self._init_by_creating_new(title, attributes)

    def _init_by_loading_existing_directory(self, folder_path):
        self._hosts = None
        self._load()
        self.load_subfolders()

    def _init_by_creating_new(self, title, attributes):
        self._hosts = {}
        self._num_hosts = 0
        self._title = title
        self._attributes = attributes
        self._locked = False
        self._locked_hosts = False
        self._locked_subfolders = False

    def __repr__(self):
        return "Folder(%r, %r)" % (self.path(), self._title)

    def get_root_dir(self):
        return self._root_dir

    # Dangerous operation! Only use this if you have a good knowledge of the internas
    def set_root_dir(self, root_dir):
        self._root_dir = root_dir.rstrip("/") + "/"  # O.o

    def parent(self):
        return self._parent

    def is_disk_folder(self):
        return True

    def _load_hosts_on_demand(self):
        if self._hosts is None:
            self._load_hosts()

    def _load_hosts(self):
        self._locked_hosts = False

        self._hosts = {}
        if not os.path.exists(self.hosts_file_path()):
            return

        variables = self._load_hosts_file()
        # Can either be set to True or a string (which will be used as host lock message)
        self._locked_hosts = variables["_lock"]

        # Add entries in clusters{} to all_hosts, prepare cluster to node mapping
        nodes_of = {}
        for cluster_with_tags, nodes in variables["clusters"].items():
            variables["all_hosts"].append(cluster_with_tags)
            nodes_of[cluster_with_tags.split('|')[0]] = nodes

        # Build list of individual hosts
        for host_name_with_tags in variables["all_hosts"]:
            parts = host_name_with_tags.split('|')
            host_name = parts[0]
            host = self._create_host_from_variables(host_name, nodes_of, variables)
            self._hosts[host_name] = host

    def _create_host_from_variables(self, host_name, nodes_of, variables):
        cluster_nodes = nodes_of.get(host_name)

        # If we have a valid entry in host_attributes then the hosts.mk file contained
        # valid WATO information from a last save and we use that
        if host_name in variables["host_attributes"]:
            attributes = variables["host_attributes"][host_name]
            attributes = self._transform_old_attributes(attributes)

        else:
            # Otherwise it is an import from some manual old version of from some
            # CMDB and we reconstruct the attributes. That way the folder inheritance
            # information is not available and all tags are set explicitely
            # 1.6: Tag transform from all_hosts has been dropped
            attributes = {}
            alias = self._get_alias_from_extra_conf(host_name, variables)
            if alias is not None:
                attributes["alias"] = alias
            for attribute_key, config_dict in [
                ("ipaddress", "ipaddresses"),
                ("ipv6address", "ipv6addresses"),
                ("snmp_community", "explicit_snmp_communities"),
            ]:
                if host_name in variables[config_dict]:
                    attributes[attribute_key] = variables[config_dict][host_name]

        return Host(self, host_name, attributes, cluster_nodes)

    def _transform_old_attributes(self, attributes):
        """Mangle all attribute structures read from the disk to prepare it for the current logic"""
        attributes = self._transform_pre_15_agent_type_in_attributes(attributes)
        attributes = self._transform_none_value_site_attribute(attributes)
        attributes = self._add_missing_meta_data(attributes)
        attributes = self._transform_tag_snmp_ds(attributes)
        return attributes

    # In versions previous to 1.6 Checkmk had a tag group named "snmp" and an
    # auxiliary tag named "snmp" in the builtin tags. This name conflict had to
    # be resolved. The tag group has been changed to "snmp_ds" to fix it.
    def _transform_tag_snmp_ds(self, attributes):
        if "tag_snmp" in attributes:
            attributes["tag_snmp_ds"] = attributes.pop("tag_snmp")
        return attributes

    # 1.6 introduced meta_data for hosts and folders to keep information about their
    # creation time. Populate this attribute for existing objects with empty data.
    def _add_missing_meta_data(self, attributes):
        attributes.setdefault("meta_data", {
            "created_at": None,
            "created_by": None,
        })
        return attributes

    # Old tag group trans:
    #('agent', u'Agent type',
    #    [
    #        ('cmk-agent', u'Check_MK Agent (Server)', ['tcp']),
    #        ('snmp-only', u'SNMP (Networking device, Appliance)', ['snmp']),
    #        ('snmp-v1',   u'Legacy SNMP device (using V1)', ['snmp']),
    #        ('snmp-tcp',  u'Dual: Check_MK Agent + SNMP', ['snmp', 'tcp']),
    #        ('ping',      u'No Agent', []),
    #    ],
    #)
    #
    def _transform_pre_15_agent_type_in_attributes(self, attributes):
        if "tag_agent" not in attributes:
            return attributes  # Nothing set here, no transformation necessary

        if "tag_snmp" in attributes:
            return attributes  # Already in new format, no transformation necessary

        value = attributes["tag_agent"]

        if value == "cmk-agent":
            attributes["tag_snmp"] = "no-snmp"

        elif value == "snmp-only":
            attributes["tag_agent"] = "no-agent"
            attributes["tag_snmp"] = "snmp-v2"

        elif value == "snmp-v1":
            attributes["tag_agent"] = "no-agent"
            attributes["tag_snmp"] = "snmp-v1"

        elif value == "snmp-tcp":
            attributes["tag_agent"] = "cmk-agent"
            attributes["tag_snmp"] = "snmp-v2"

        elif value == "ping":
            attributes["tag_agent"] = "no-agent"
            attributes["tag_snmp"] = "no-snmp"

        return attributes

    def _transform_none_value_site_attribute(self, attributes):
        # Old WATO was saving "site" attribute with value of None. Skip this key.
        if "site" in attributes and attributes["site"] is None:
            del attributes["site"]
        return attributes

    def _load_hosts_file(self):
        variables = {
            "FOLDER_PATH": "",
            "ALL_HOSTS": ALL_HOSTS,
            "ALL_SERVICES": ALL_SERVICES,
            "all_hosts": [],
            "host_labels": {},
            "host_tags": {},
            "clusters": {},
            "ipaddresses": {},
            "ipv6addresses": {},
            "explicit_snmp_communities": {},
            "management_snmp_credentials": {},
            "management_ipmi_credentials": {},
            "management_protocol": {},
            "extra_host_conf": {
                "alias": []
            },
            "extra_service_conf": {
                "_WATO": []
            },
            "host_attributes": {},
            "host_contactgroups": [],
            "service_contactgroups": [],
            "_lock": False,
        }
        return store.load_mk_file(self.hosts_file_path(), variables)

    def save_hosts(self):
        self.need_unlocked_hosts()
        self.need_permission("write")
        if self._hosts is not None:
            self._save_hosts_file()

            # Clean up caches of all hosts in this folder, just to be sure. We could also
            # check out all call sites of save_hosts() and partially drop the caches of
            # individual hosts to optimize this.
            for host in self._hosts.values():
                host.drop_caches()

        call_hook_hosts_changed(self)

    def _save_hosts_file(self):
        self._ensure_folder_directory()
        if not self.has_hosts():
            if os.path.exists(self.hosts_file_path()):
                os.remove(self.hosts_file_path())
            return

        out = cStringIO.StringIO()
        out.write(wato_fileheader())

        all_hosts = []  # type: List[str]
        clusters = {}  # type: Dict[str, List[str]]
        hostnames = self.hosts().keys()
        hostnames.sort()
        custom_macros = {}  # collect value for attributes that are to be present in Nagios
        cleaned_hosts = {}
        host_tags = {}
        host_labels = {}

        attribute_mappings = [
            # host attr, cmk_base variable name, value, title
            ("ipaddress", "ipaddresses", {}, "Explicit IPv4 addresses"),
            ("ipv6address", "ipv6addresses", {}, "Explicit IPv6 addresses"),
            ("snmp_community", "explicit_snmp_communities", {}, "Explicit SNMP communities"),
            ("management_snmp_community", "management_snmp_credentials", {},
             "Management board SNMP credentials"),
            ("management_ipmi_credentials", "management_ipmi_credentials", {},
             "Management board IPMI credentials"),
            ("management_protocol", "management_protocol", {}, "Management board protocol"),
        ]

        for hostname in hostnames:
            host = self.hosts()[hostname]
            effective = host.effective_attributes()
            cleaned_hosts[hostname] = host.attributes()

            tag_groups = host.tag_groups()
            if tag_groups:
                host_tags[hostname] = tag_groups

            labels = host.labels()
            if labels:
                host_labels[hostname] = labels

            if host.is_cluster():
                clusters[hostname] = host.cluster_nodes()
            else:
                all_hosts.append(hostname)

            # Save the effective attributes of a host to the related attribute maps.
            # These maps are saved directly in the hosts.mk to transport the effective
            # attributes to Check_MK base.
            for attribute_name, _unused_cmk_var_name, dictionary, _unused_title in attribute_mappings:
                value = effective.get(attribute_name)
                if value:
                    dictionary[hostname] = value

            # Create contact group rule entries for hosts with explicitely set values
            # Note: since the type if this entry is a list, not a single contact group, all other list
            # entries coming after this one will be ignored. That way the host-entries have
            # precedence over the folder entries.

            if host.has_explicit_attribute("contactgroups"):
                cgconfig = convert_cgroups_from_tuple(host.attribute("contactgroups"))
                cgs = cgconfig["groups"]
                if cgs and cgconfig["use"]:
                    out.write("\nhost_contactgroups += [\n")
                    for cg in cgs:
                        out.write('    ( %r, [%r] ),\n' % (cg, hostname))
                    out.write(']\n\n')

                    if cgconfig.get("use_for_services"):
                        out.write("\nservice_contactgroups += [\n")
                        for cg in cgs:
                            out.write('    ( %r, [%r], ALL_SERVICES ),\n' % (cg, hostname))
                        out.write(']\n\n')

            for attr in host_attribute_registry.attributes():
                attrname = attr.name()
                if attrname in effective:
                    custom_varname = attr.nagios_name()
                    if custom_varname:
                        value = effective.get(attrname)
                        nagstring = attr.to_nagios(value)
                        if nagstring is not None:
                            if custom_varname not in custom_macros:
                                custom_macros[custom_varname] = {}
                            custom_macros[custom_varname][hostname] = nagstring

        if all_hosts:
            out.write("all_hosts += %s\n" % format_config_value(all_hosts))

        if clusters:
            out.write("\nclusters.update(%s)\n" % format_config_value(clusters))

        out.write("\nhost_tags.update(%s)\n" % format_config_value(host_tags))

        out.write("\nhost_labels.update(%s)\n" % format_config_value(host_labels))

        for attribute_name, cmk_base_varname, dictionary, title in attribute_mappings:
            if dictionary:
                out.write("\n# %s\n" % title)
                out.write("%s.update(" % cmk_base_varname)
                out.write(format_config_value(dictionary))
                out.write(")\n")

        for custom_varname, entries in custom_macros.items():
            macrolist = []
            for hostname, nagstring in entries.items():
                macrolist.append((nagstring, [hostname]))
            if len(macrolist) > 0:
                out.write("\n# Settings for %s\n" % custom_varname)
                out.write("extra_host_conf.setdefault(%r, []).extend(\n" % custom_varname)
                out.write("  %s)\n" % format_config_value(macrolist))

        # If the contact groups of the host are set to be used for the monitoring,
        # we create an according rule for the folder and an according rule for
        # each host that has an explicit setting for that attribute.
        _permitted_groups, contact_groups, use_for_services = self.groups()
        if contact_groups:
            out.write("\nhost_contactgroups.append(\n"
                      "  ( %r, [ '/' + FOLDER_PATH + '/' ], ALL_HOSTS ))\n" % list(contact_groups))
            if use_for_services:
                # Currently service_contactgroups requires single values. Lists are not supported
                for cg in contact_groups:
                    out.write(
                        "\nservice_contactgroups.append(\n"
                        "  ( %r, [ '/' + FOLDER_PATH + '/' ], ALL_HOSTS, ALL_SERVICES ))\n" % cg)

        # Write information about all host attributes into special variable - even
        # values stored for check_mk as well.
        out.write("\n# Host attributes (needed for WATO)\n")
        out.write("host_attributes.update(\n%s)\n" % format_config_value(cleaned_hosts))

        store.save_file(self.hosts_file_path(), out.getvalue())

    def _get_alias_from_extra_conf(self, host_name, variables):
        aliases = self._host_extra_conf(host_name, variables["extra_host_conf"]["alias"])
        if len(aliases) > 0:
            return aliases[0]
        return

    # This is a dummy implementation which works without tags
    # and implements only a special case of Check_MK's real logic.
    def _host_extra_conf(self, host_name, conflist):
        for value, hostlist in conflist:
            if host_name in hostlist:
                return [value]
        return []

    def _load(self):
        wato_info = self._load_wato_info()
        self._title = wato_info.get("title", self._fallback_title())
        self._attributes = self._transform_old_attributes(wato_info.get("attributes", {}))
        # Can either be set to True or a string (which will be used as host lock message)
        self._locked = wato_info.get("lock", False)
        # Can either be set to True or a string (which will be used as host lock message)
        self._locked_subfolders = wato_info.get("lock_subfolders", False)

        if "num_hosts" in wato_info:
            self._num_hosts = wato_info.get("num_hosts", None)
        else:
            self._num_hosts = len(self.hosts())
            self._save_wato_info()

    def _load_wato_info(self):
        return store.load_data_from_file(self.wato_info_path(), {})

    def save(self):
        self._save_wato_info()
        Folder.invalidate_caches()

    def _save_wato_info(self):
        self._ensure_folder_directory()
        store.save_data_to_file(self.wato_info_path(), self.get_wato_info())

    def get_wato_info(self):
        return {
            "title": self._title,
            "attributes": self._attributes,
            "num_hosts": self._num_hosts,
            "lock": self._locked,
            "lock_subfolders": self._locked_subfolders,
        }

    def _ensure_folder_directory(self):
        store.makedirs(self.filesystem_path())

    def _fallback_title(self):
        if self.is_root():
            return _("Main directory")
        return self.name()

    def load_subfolders(self):
        dir_path = self._root_dir + self.path()
        for entry in os.listdir(dir_path):
            subfolder_dir = dir_path + "/" + entry
            if os.path.isdir(subfolder_dir):
                if self.path():
                    subfolder_path = self.path() + "/" + entry
                else:
                    subfolder_path = entry
                self._subfolders[entry] = Folder(
                    entry, subfolder_path, self, root_dir=self._root_dir)

    def wato_info_path(self):
        return self.filesystem_path() + "/.wato"

    def hosts_file_path(self):
        return self.filesystem_path() + "/hosts.mk"

    def rules_file_path(self):
        return self.filesystem_path() + "/rules.mk"

    def add_to_dictionary(self, dictionary):
        dictionary[self.path()] = self
        for subfolder in self._subfolders.values():
            subfolder.add_to_dictionary(dictionary)

    def drop_caches(self):
        super(CREFolder, self).drop_caches()
        self._choices_for_moving_host = None

        for subfolder in self._subfolders.values():
            subfolder.drop_caches()

        if self._hosts is not None:
            for host in self._hosts.values():
                host.drop_caches()

    # .-----------------------------------------------------------------------.
    # | ELEMENT ACCESS                                                        |
    # '-----------------------------------------------------------------------'

    def name(self):
        return self._name

    def title(self):
        return self._title

    def filesystem_path(self):
        return (self._root_dir + self.path()).rstrip("/")

    def ident(self):
        return self.path()

    def path(self):
        if self.is_root():
            return ""
        elif self.parent().is_root():
            return self.name()
        return self.parent().path() + "/" + self.name()

    def linkinfo(self):
        return self.path() + ":"

    def hosts(self):
        self._load_hosts_on_demand()
        return self._hosts

    def num_hosts(self):
        # Do *not* load hosts here! This method must kept cheap
        return self._num_hosts

    def num_hosts_recursively(self):
        num = self.num_hosts()
        for subfolder in self.visible_subfolders().values():
            num += subfolder.num_hosts_recursively()
        return num

    def all_hosts_recursively(self):
        hosts = {}
        hosts.update(self.hosts())
        for subfolder in self.all_subfolders().values():
            hosts.update(subfolder.all_hosts_recursively())
        return hosts

    def visible_subfolders(self):
        visible_folders = {}
        for folder_name, folder in self._subfolders.items():
            if folder.folder_should_be_shown("read"):
                visible_folders[folder_name] = folder

        return visible_folders

    def all_subfolders(self):
        return self._subfolders

    def subfolder(self, name):
        return self._subfolders[name]

    def subfolder_by_title(self, title):
        for subfolder in self.all_subfolders().values():
            if subfolder.title() == title:
                return subfolder

    def has_subfolder(self, name):
        return name in self._subfolders

    def has_subfolders(self):
        return len(self._subfolders) > 0

    def subfolder_choices(self):
        choices = []
        for subfolder in self.visible_subfolders_sorted_by_title():
            choices.append((subfolder.path(), subfolder.title()))
        return choices

    def recursive_subfolder_choices(self, current_depth=0, pretty=True):
        if pretty:
            if current_depth:
                title_prefix = (u"\u00a0" * 6 * current_depth) + u"\u2514\u2500 "
            else:
                title_prefix = ""
            title = HTML(title_prefix + html.attrencode(self.title()))
        else:
            title = HTML(html.attrencode("/".join(self.title_path_without_root())))

        sel = [(self.path(), title)]

        for subfolder in self.visible_subfolders_sorted_by_title():
            sel += subfolder.recursive_subfolder_choices(current_depth + 1, pretty)
        return sel

    def choices_for_moving_folder(self):
        return self._choices_for_moving("folder")

    def choices_for_moving_host(self):
        if self._choices_for_moving_host is not None:
            return self._choices_for_moving_host  # Cached

        self._choices_for_moving_host = self._choices_for_moving("host")
        return self._choices_for_moving_host

    def folder_should_be_shown(self, how):
        if not config.wato_hide_folders_without_read_permissions:
            return True

        has_permission = self.may(how)
        for subfolder in self.all_subfolders().values():
            if has_permission:
                break
            has_permission = subfolder.folder_should_be_shown(how)

        return has_permission

    def _choices_for_moving(self, what):
        choices = []

        for folder_path, folder in Folder.all_folders().items():
            if not folder.may("write"):
                continue
            if folder.is_same_as(self):
                continue  # do not move into itself

            if what == "folder":
                if folder.is_same_as(self.parent()):
                    continue  # We are already in that folder
                if folder.name() in folder.all_subfolders():
                    continue  # naming conflict
                if self.is_transitive_parent_of(folder):
                    continue  # we cannot be moved in our child folder

            msg = "/".join(folder.title_path_without_root())
            choices.append((folder_path, msg))

        choices.sort(cmp=lambda a, b: cmp(a[1].lower(), b[1].lower()))
        return choices

    def subfolders_sorted_by_title(self):
        return sorted(self.all_subfolders().values(), cmp=lambda a, b: cmp(a.title(), b.title()))

    def visible_subfolders_sorted_by_title(self):
        return sorted(
            self.visible_subfolders().values(), cmp=lambda a, b: cmp(a.title(), b.title()))

    def site_id(self):
        if "site" in self._attributes:
            return self._attributes["site"]
        elif self.has_parent():
            return self.parent().site_id()
        return default_site()

    def all_site_ids(self):
        site_ids = set()
        self._add_all_sites_to_set(site_ids)
        return list(site_ids)

    def title_path(self, withlinks=False):
        titles = []
        for folder in self.parent_folder_chain() + [self]:
            title = folder.title()
            if withlinks:
                title = "<a href='wato.py?mode=folder&folder=%s'>%s</a>" % (folder.path(), title)
            titles.append(title)
        return titles

    def title_path_without_root(self):
        if self.is_root():
            return [self.title()]
        return self.title_path()[1:]

    def alias_path(self, show_main=True):
        if show_main:
            return " / ".join(self.title_path())
        return " / ".join(self.title_path_without_root())

    def effective_attributes(self):
        try:
            return self._get_cached_effective_attributes()  # cached :-)
        except KeyError:
            pass

        effective = {}
        for folder in self.parent_folder_chain():
            effective.update(folder.attributes())
        effective.update(self.attributes())

        # now add default values of attributes for all missing values
        for host_attribute in host_attribute_registry.attributes():
            attrname = host_attribute.name()
            if attrname not in effective:
                effective.setdefault(attrname, host_attribute.default_value())

        self._cache_effective_attributes(effective)
        return effective

    def groups(self, host=None):
        # CLEANUP: this method is also used for determining host permission
        # in behalv of Host::groups(). Not nice but was done for avoiding
        # code duplication
        permitted_groups = set([])
        host_contact_groups = set([])
        if host:
            effective_folder_attributes = host.effective_attributes()
        else:
            effective_folder_attributes = self.effective_attributes()
        cgconf = self._get_cgconf_from_attributes(effective_folder_attributes)

        # First set explicit groups
        permitted_groups.update(cgconf["groups"])
        if cgconf["use"]:
            host_contact_groups.update(cgconf["groups"])

        if host:
            parent = self
        else:
            parent = self.parent()

        while parent:
            effective_folder_attributes = parent.effective_attributes()
            parconf = self._get_cgconf_from_attributes(effective_folder_attributes)
            parent_permitted_groups, parent_host_contact_groups, _parent_use_for_services = parent.groups(
            )

            if parconf["recurse_perms"]:  # Parent gives us its permissions
                permitted_groups.update(parent_permitted_groups)

            if parconf["recurse_use"]:  # Parent give us its contact groups
                host_contact_groups.update(parent_host_contact_groups)

            parent = parent.parent()

        return permitted_groups, host_contact_groups, cgconf.get("use_for_services", False)

    def find_host_recursively(self, host_name):
        host = self.host(host_name)
        if host:
            return host

        for subfolder in self.all_subfolders().values():
            host = subfolder.find_host_recursively(host_name)
            if host:
                return host

    def _user_needs_permission(self, how):
        if how == "write" and config.user.may("wato.all_folders"):
            return

        if how == "read" and config.user.may("wato.see_all_folders"):
            return

        permitted_groups, _folder_contactgroups, _use_for_services = self.groups()
        user_contactgroups = userdb.contactgroups_of_user(config.user.id)

        for c in user_contactgroups:
            if c in permitted_groups:
                return

        reason = _("Sorry, you have no permissions to the folder <b>%s</b>.") % self.alias_path()
        if not permitted_groups:
            reason += " " + _("The folder is not permitted for any contact group.")
        else:
            reason += " " + _("The folder's permitted contact groups are <b>%s</b>.") % ", ".join(
                permitted_groups)
            if user_contactgroups:
                reason += " " + _("Your contact groups are <b>%s</b>.") % ", ".join(
                    user_contactgroups)
            else:
                reason += " " + _("But you are not a member of any contact group.")
        reason += " " + _(
            "You may enter the folder as you might have permission on a subfolders, though.")
        raise MKAuthException(reason)

    def need_recursive_permission(self, how):
        self.need_permission(how)
        if how == "write":
            self.need_unlocked()
            self.need_unlocked_subfolders()
            self.need_unlocked_hosts()

        for subfolder in self.all_subfolders().values():
            subfolder.need_recursive_permission(how)

    def need_unlocked(self):
        if self.locked():
            raise MKAuthException(
                _("Sorry, you cannot edit the folder %s. It is locked.") % self.title())

    def need_unlocked_hosts(self):
        if self.locked_hosts():
            raise MKAuthException(_("Sorry, the hosts in the folder %s are locked.") % self.title())

    def need_unlocked_subfolders(self):
        if self.locked_subfolders():
            raise MKAuthException(
                _("Sorry, the sub folders in the folder %s are locked.") % self.title())

    def url(self, add_vars=None):
        if add_vars is None:
            add_vars = []

        url_vars = [("folder", self.path())]
        have_mode = False
        for varname, _value in add_vars:
            if varname == "mode":
                have_mode = True
                break
        if not have_mode:
            url_vars.append(("mode", "folder"))
        if html.request.var("debug") == "1":
            add_vars.append(("debug", "1"))
        url_vars += add_vars
        return html.makeuri_contextless(url_vars, filename="wato.py")

    def edit_url(self, backfolder=None):
        if backfolder is None:
            if self.has_parent():
                backfolder = self.parent()
            else:
                backfolder = self
        return html.makeuri_contextless([
            ("mode", "editfolder"),
            ("folder", self.path()),
            ("backfolder", backfolder.path()),
        ])

    def locked(self):
        return self._locked

    def locked_subfolders(self):
        return self._locked_subfolders

    def locked_hosts(self):
        self._load_hosts_on_demand()
        return self._locked_hosts

    # Returns:
    #  None:      No network scan is enabled.
    #  timestamp: Next planned run according to config.
    def next_network_scan_at(self):
        if "network_scan" not in self._attributes:
            return

        interval = self._attributes["network_scan"]["scan_interval"]
        last_end = self._attributes.get("network_scan_result", {}).get("end", None)
        if last_end is None:
            next_time = time.time()
        else:
            next_time = last_end + interval

        time_allowed = self._attributes["network_scan"].get("time_allowed")
        if time_allowed is None:
            return next_time  # No time frame limit

        # Transform pre 1.6 single time window to list of time windows
        times_allowed = [time_allowed] if isinstance(time_allowed, tuple) else time_allowed

        # Compute "next time" with all time windows individually and use earliest time
        next_allowed_times = []
        for time_allowed in times_allowed:
            # First transform the time given by the user to UTC time
            brokentime = list(time.localtime(next_time))
            brokentime[3], brokentime[4] = time_allowed[0]
            start_time = time.mktime(brokentime)

            brokentime[3], brokentime[4] = time_allowed[1]
            end_time = time.mktime(brokentime)

            # In case the next time is earlier than the allowed time frame at a day set
            # the time to the time frame start.
            # In case the next time is in the time frame leave it at it's value.
            # In case the next time is later then advance one day to the start of the
            # time frame.
            if next_time < start_time:
                next_allowed_times.append(start_time)
            elif next_time > end_time:
                next_allowed_times.append(start_time + 86400)
            else:
                next_allowed_times.append(next_time)

        return min(next_allowed_times)

    # .-----------------------------------------------------------------------.
    # | MODIFICATIONS                                                         |
    # |                                                                       |
    # | These methods are for being called by actual WATO modules when they   |
    # | want to modify folders and hosts. They all check permissions and      |
    # | locking. They may raise MKAuthException or MKUserError.               |
    # |                                                                       |
    # | Folder permissions: Creation and deletion of subfolders needs write   |
    # | permissions in the parent folder (like in Linux).                     |
    # |                                                                       |
    # | Locking: these methods also check locking. Locking is for preventing  |
    # | changes in files that are created by third party applications.        |
    # | A folder has three lock attributes:                                   |
    # |                                                                       |
    # | - locked_hosts() -> hosts.mk file in the folder must not be modified  |
    # | - locked()       -> .wato file in the folder must not be modified     |
    # | - locked_subfolders() -> No subfolders may be created/deleted         |
    # |                                                                       |
    # | Sidebar: some sidebar snapins show the WATO folder tree. Everytime    |
    # | the tree changes the sidebar needs to be reloaded. This is done here. |
    # |                                                                       |
    # | Validation: these methods do *not* validate the parameters for syntax.|
    # | This is the task of the actual WATO modes or the API.                 |
    # '-----------------------------------------------------------------------'

    def create_subfolder(self, name, title, attributes):
        # 1. Check preconditions
        config.user.need_permission("wato.manage_folders")
        self.need_permission("write")
        self.need_unlocked_subfolders()
        must_be_in_contactgroups(attributes.get("contactgroups"))

        attributes.setdefault("meta_data", get_meta_data(created_by=config.user.id))

        # 2. Actual modification
        new_subfolder = Folder(name, parent_folder=self, title=title, attributes=attributes)
        self._subfolders[name] = new_subfolder
        new_subfolder.save()
        add_change(
            "new-folder",
            _("Created new folder %s") % new_subfolder.alias_path(),
            obj=new_subfolder,
            sites=[new_subfolder.site_id()])
        hooks.call("folder-created", new_subfolder)
        need_sidebar_reload()
        return new_subfolder

    def delete_subfolder(self, name):
        # 1. Check preconditions
        config.user.need_permission("wato.manage_folders")
        self.need_permission("write")
        self.need_unlocked_subfolders()

        # 2. check if hosts have parents
        subfolder = self.subfolder(name)
        hosts_with_children = self._get_parents_of_hosts(subfolder.all_hosts_recursively().keys())
        if hosts_with_children:
            raise MKUserError("delete_host", _("You cannot delete these hosts: %s") % \
                              ", ".join([_("%s is parent of %s.") % (parent, ", ".join(children))
                              for parent, children in sorted(hosts_with_children.items())]))

        # 3. Actual modification
        hooks.call("folder-deleted", subfolder)
        add_change(
            "delete-folder",
            _("Deleted folder %s") % subfolder.alias_path(),
            obj=self,
            sites=subfolder.all_site_ids())
        self._remove_subfolder(name)
        shutil.rmtree(subfolder.filesystem_path())
        Folder.invalidate_caches()
        need_sidebar_reload()

    def move_subfolder_to(self, subfolder, target_folder):
        # 1. Check preconditions
        config.user.need_permission("wato.manage_folders")
        self.need_permission("write")
        self.need_unlocked_subfolders()
        target_folder.need_permission("write")
        target_folder.need_unlocked_subfolders()
        subfolder.need_recursive_permission("write")  # Inheritance is changed
        if os.path.exists(target_folder.filesystem_path() + "/" + subfolder.name()):
            raise MKUserError(
                None,
                _("Cannot move folder: A folder with this name already exists in the target folder."
                 ))

        original_alias_path = subfolder.alias_path()

        # 2. Actual modification
        affected_sites = subfolder.all_site_ids()
        old_filesystem_path = subfolder.filesystem_path()
        del self._subfolders[subfolder.name()]
        subfolder._parent = target_folder
        target_folder._subfolders[subfolder.name()] = subfolder
        shutil.move(old_filesystem_path, subfolder.filesystem_path())
        subfolder.rewrite_hosts_files()  # fixes changed inheritance
        Folder.invalidate_caches()
        affected_sites = list(set(affected_sites + subfolder.all_site_ids()))
        add_change(
            "move-folder",
            _("Moved folder %s to %s") % (original_alias_path, target_folder.alias_path()),
            obj=subfolder,
            sites=affected_sites)
        need_sidebar_reload()

    def edit(self, new_title, new_attributes):
        # 1. Check preconditions
        self.need_permission("write")
        self.need_unlocked()

        # For changing contact groups user needs write permission on parent folder
        if self._get_cgconf_from_attributes(new_attributes) != \
           self._get_cgconf_from_attributes(self.attributes()):
            must_be_in_contactgroups(self.attributes().get("contactgroups"))
            if self.has_parent():
                if not self.parent().may("write"):
                    raise MKAuthException(
                        _("Sorry. In order to change the permissions of a folder you need write "
                          "access to the parent folder."))

        # 2. Actual modification

        # Due to a change in the attribute "site" a host can move from
        # one site to another. In that case both sites need to be marked
        # dirty. Therefore we first mark dirty according to the current
        # host->site mapping and after the change we mark again according
        # to the new mapping.
        affected_sites = self.all_site_ids()

        self._title = new_title
        self._attributes = new_attributes

        # Due to changes in folder/file attributes, host files
        # might need to be rewritten in order to reflect Changes
        # in Nagios-relevant attributes.
        self.save()
        self.rewrite_hosts_files()

        affected_sites = list(set(affected_sites + self.all_site_ids()))
        add_change(
            "edit-folder",
            _("Edited properties of folder %s") % self.title(),
            obj=self,
            sites=affected_sites)

    def _get_cgconf_from_attributes(self, attributes):
        v = attributes.get("contactgroups", (False, []))
        cgconf = convert_cgroups_from_tuple(v)
        return cgconf

    def create_hosts(self, entries):
        # 1. Check preconditions
        config.user.need_permission("wato.manage_hosts")
        self.need_unlocked_hosts()
        self.need_permission("write")

        for host_name, attributes, cluster_nodes in entries:
            must_be_in_contactgroups(attributes.get("contactgroups"))
            validate_host_uniqueness("host", host_name)

            attributes.setdefault("meta_data", get_meta_data(created_by=config.user.id))

        # 2. Actual modification
        self._load_hosts_on_demand()
        for host_name, attributes, cluster_nodes in entries:
            host = Host(self, host_name, attributes, cluster_nodes)
            self._hosts[host_name] = host
            self._num_hosts = len(self._hosts)
            add_change(
                "create-host",
                _("Created new host %s.") % host_name,
                obj=host,
                sites=[host.site_id()])
        self._save_wato_info()  # num_hosts has changed
        self.save_hosts()

    def delete_hosts(self, host_names):
        # 1. Check preconditions
        config.user.need_permission("wato.manage_hosts")
        self.need_unlocked_hosts()
        self.need_permission("write")

        # 2. check if hosts have parents
        hosts_with_children = self._get_parents_of_hosts(host_names)
        if hosts_with_children:
            raise MKUserError("delete_host", _("You cannot delete these hosts: %s") % \
                              ", ".join([_("%s is parent of %s.") % (parent, ", ".join(children))
                              for parent, children in sorted(hosts_with_children.items())]))

        # 3. Delete host specific files (caches, tempfiles, ...)
        self._delete_host_files(host_names)

        # 4. Actual modification
        for host_name in host_names:
            host = self.hosts()[host_name]
            del self._hosts[host_name]
            self._num_hosts = len(self._hosts)
            add_change(
                "delete-host", _("Deleted host %s") % host_name, obj=host, sites=[host.site_id()])

        self._save_wato_info()  # num_hosts has changed
        self.save_hosts()

    def _get_parents_of_hosts(self, host_names):
        # Note: Deletion of chosen hosts which are parents
        # is possible if and only if all children are chosen, too.
        hosts_with_children = {}
        for child_key, child in Folder.root_folder().all_hosts_recursively().items():
            for host_name in host_names:
                if host_name in child.parents():
                    hosts_with_children.setdefault(host_name, [])
                    hosts_with_children[host_name].append(child_key)

        result = {}
        for parent, children in hosts_with_children.items():
            if not set(children) < set(host_names):
                result.setdefault(parent, children)
        return result

    # Group the given host names by their site and delete their files
    def _delete_host_files(self, host_names):
        hosts_by_site = {}
        for host_name in host_names:
            host = self.hosts()[host_name]
            hosts_by_site.setdefault(host.site_id(), []).append(host_name)

        for site_id, site_host_names in hosts_by_site.items():
            check_mk_automation(site_id, "delete-hosts", site_host_names)

    def move_hosts(self, host_names, target_folder):
        # 1. Check preconditions
        config.user.need_permission("wato.manage_hosts")
        config.user.need_permission("wato.edit_hosts")
        config.user.need_permission("wato.move_hosts")
        self.need_permission("write")
        self.need_unlocked_hosts()
        target_folder.need_permission("write")
        target_folder.need_unlocked_hosts()

        # 2. Actual modification
        for host_name in host_names:
            host = self.host(host_name)

            affected_sites = [host.site_id()]

            self._remove_host(host)
            target_folder._add_host(host)

            affected_sites = list(set(affected_sites + [host.site_id()]))
            add_change(
                "move-host",
                _("Moved host from %s to %s") % (self.path(), target_folder.path()),
                obj=host,
                sites=affected_sites)

        self._save_wato_info()  # num_hosts has changed
        self.save_hosts()
        target_folder._save_wato_info()
        target_folder.save_hosts()

    def rename_host(self, oldname, newname):
        # 1. Check preconditions
        config.user.need_permission("wato.manage_hosts")
        config.user.need_permission("wato.edit_hosts")
        self.need_unlocked_hosts()
        host = self.hosts()[oldname]
        host.need_permission("write")

        # 2. Actual modification
        host.rename(newname)
        del self._hosts[oldname]
        self._hosts[newname] = host
        add_change(
            "rename-host",
            _("Renamed host from %s to %s") % (oldname, newname),
            obj=host,
            sites=[host.site_id()])
        self.save_hosts()

    def rename_parent(self, oldname, newname):
        # Must not fail because of auth problems. Auth is check at the
        # actually renamed host.
        changed = rename_host_in_list(self._attributes["parents"], oldname, newname)
        if not changed:
            return False

        add_change(
            "rename-parent",
            _("Renamed parent from %s to %s in folder \"%s\"") % (oldname, newname,
                                                                  self.alias_path()),
            obj=self,
            sites=self.all_site_ids())
        self.save_hosts()
        self.save()
        return True

    def rewrite_hosts_files(self):
        self._rewrite_hosts_file()
        for subfolder in self.all_subfolders().values():
            subfolder.rewrite_hosts_files()

    def _add_host(self, host):
        self._load_hosts_on_demand()
        self._hosts[host.name()] = host
        host._folder = self
        self._num_hosts = len(self._hosts)

    def _remove_host(self, host):
        self._load_hosts_on_demand()
        del self._hosts[host.name()]
        host._folder = None
        self._num_hosts = len(self._hosts)

    def _remove_subfolder(self, name):
        del self._subfolders[name]

    def _add_all_sites_to_set(self, site_ids):
        site_ids.add(self.site_id())
        for host in self.hosts().values():
            site_ids.add(host.site_id())
        for subfolder in self.all_subfolders().values():
            subfolder._add_all_sites_to_set(site_ids)

    def _rewrite_hosts_file(self):
        self._load_hosts_on_demand()
        self.save_hosts()

    # .-----------------------------------------------------------------------.
    # | HTML Generation                                                       |
    # '-----------------------------------------------------------------------'

    def show_locking_information(self):
        self._load_hosts_on_demand()
        lock_messages = []

        # Locked hosts
        if self._locked_hosts is True:
            lock_messages.append(
                _("Host attributes are locked "
                  "(You cannot create, edit or delete hosts in this folder)"))
        elif self._locked_hosts:
            lock_messages.append(self._locked_hosts)

        # Locked folder attributes
        if self._locked is True:
            lock_messages.append(
                _("Folder attributes are locked "
                  "(You cannot edit the attributes of this folder)"))
        elif self._locked:
            lock_messages.append(self._locked)

        # Also subfolders are locked
        if self._locked_subfolders:
            lock_messages.append(
                _("Subfolders are locked "
                  "(You cannot create or remove folders in this folder)"))
        elif self._locked_subfolders:
            lock_messages.append(self._locked_subfolders)

        if lock_messages:
            if len(lock_messages) == 1:
                lock_message = lock_messages[0]
            else:
                li_elements = "".join(["<li>%s</li>" % m for m in lock_messages])
                lock_message = "<ul>" + li_elements + "</ul>"
            html.show_info(lock_message)


def validate_host_uniqueness(varname, host_name):
    host = Host.host(host_name)
    if host:
        raise MKUserError(
            varname,
            _('A host with the name <b><tt>%s</tt></b> already '
              'exists in the folder <a href="%s">%s</a>.') % (host_name, host.folder().url(),
                                                              host.folder().alias_path()))


class SearchFolder(BaseFolder):
    """A virtual folder representing the result of a search."""

    @staticmethod
    def criteria_from_html_vars():
        crit = {".name": html.request.var("host_search_host")}
        crit.update(
            cmk.gui.watolib.host_attributes.collect_attributes(
                "host_search", new=False, do_validate=False, varprefix="host_search_"))
        return crit

    # This method is allowed to return None when no search is currently performed.
    @staticmethod
    def current_search_folder():
        if html.request.has_var("host_search"):
            base_folder = Folder.folder(html.request.var("folder", ""))
            search_criteria = SearchFolder.criteria_from_html_vars()
            folder = SearchFolder(base_folder, search_criteria)
            Folder.set_current(folder)
            return folder

    # .--------------------------------------------------------------------.
    # | CONSTRUCTION                                                       |
    # '--------------------------------------------------------------------'

    def __init__(self, base_folder, criteria):
        super(SearchFolder, self).__init__()
        self._criteria = criteria
        self._base_folder = base_folder
        self._found_hosts = None
        self._name = None

    def __repr__(self):
        return "SearchFolder(%r, %s)" % (self._base_folder.path(), self._name)

    # .--------------------------------------------------------------------.
    # | ACCESS                                                             |
    # '--------------------------------------------------------------------'

    def attributes(self):
        return {}

    def parent(self):
        return self._base_folder

    def is_search_folder(self):
        return True

    def _user_needs_permission(self, how):
        pass

    def title(self):
        return _("Search results for folder %s") % self._base_folder.title()

    def hosts(self):
        if self._found_hosts is None:
            self._found_hosts = self._search_hosts_recursively(self._base_folder)
        return self._found_hosts

    def locked_hosts(self):
        return False

    def locked_subfolders(self):
        return False

    def show_locking_information(self):
        pass

    def has_subfolder(self, name):
        return False

    def has_subfolders(self):
        return False

    def choices_for_moving_host(self):
        return Folder.folder_choices()

    def path(self):
        if self._name:
            return self._base_folder.path() + "//search:" + self._name
        return self._base_folder.path() + "//search"

    def url(self, add_vars=None):
        if add_vars is None:
            add_vars = []

        url_vars = [("host_search", "1")] + add_vars

        for varname, value in html.request.itervars():
            if varname.startswith("host_search_") \
                or varname.startswith("_change"):
                url_vars.append((varname, value))
        return self.parent().url(url_vars)

    # .--------------------------------------------------------------------.
    # | ACTIONS                                                            |
    # '--------------------------------------------------------------------'

    def delete_hosts(self, host_names):
        auth_errors = []
        for folder, these_host_names in self._group_hostnames_by_folder(host_names):
            try:
                folder.delete_hosts(these_host_names)
            except MKAuthException as e:
                auth_errors.append(
                    _("<li>Cannot delete hosts in folder %s: %s</li>") % (folder.alias_path(), e))
        self._invalidate_search()
        if auth_errors:
            raise MKAuthException(
                _("Some hosts could not be deleted:<ul>%s</ul>") % "".join(auth_errors))

    def move_hosts(self, host_names, target_folder):
        auth_errors = []
        for folder, host_names1 in self._group_hostnames_by_folder(host_names):
            try:
                # FIXME: this is not transaction safe, might get partially finished...
                folder.move_hosts(host_names1, target_folder)
            except MKAuthException as e:
                auth_errors.append(
                    _("<li>Cannot move hosts from folder %s: %s</li>") % (folder.alias_path(), e))
        self._invalidate_search()
        if auth_errors:
            raise MKAuthException(
                _("Some hosts could not be moved:<ul>%s</ul>") % "".join(auth_errors))

    # .--------------------------------------------------------------------.
    # | PRIVATE METHODS                                                    |
    # '--------------------------------------------------------------------'

    def _group_hostnames_by_folder(self, host_names):
        by_folder = {}
        for host_name in host_names:
            host = self.host(host_name)
            by_folder.setdefault(host.folder().path(), []).append(host)

        return [
            (hosts[0].folder(), [host.name() for host in hosts]) for hosts in by_folder.values()
        ]

    def _search_hosts_recursively(self, in_folder):
        hosts = self._search_hosts(in_folder)
        for subfolder in in_folder.all_subfolders().values():
            hosts.update(self._search_hosts_recursively(subfolder))
        return hosts

    def _search_hosts(self, in_folder):
        if not in_folder.may("read"):
            return {}

        found = {}
        for host_name, host in in_folder.hosts().items():
            if self._criteria[".name"] and not host_attribute_matches(self._criteria[".name"],
                                                                      host_name):
                continue

            # Compute inheritance
            effective = host.effective_attributes()

            # Check attributes
            dont_match = False
            for attr in host_attribute_registry.attributes():
                attrname = attr.name()
                if attrname in self._criteria and  \
                    not attr.filter_matches(self._criteria[attrname], effective.get(attrname), host_name):
                    dont_match = True
                    break

            if not dont_match:
                found[host_name] = host

        return found

    def _invalidate_search(self):
        self._found_hosts = None


class CREHost(WithPermissionsAndAttributes):
    """Class representing one host that is managed via WATO. Hosts are contained in Folders."""
    # .--------------------------------------------------------------------.
    # | STATIC METHODS                                                     |
    # '--------------------------------------------------------------------'

    @staticmethod
    def host(host_name):
        return Folder.root_folder().find_host_recursively(host_name)

    @staticmethod
    def all():
        return Folder.root_folder().all_hosts_recursively()

    @staticmethod
    def host_exists(host_name):
        return Host.host(host_name) is not None

    # .--------------------------------------------------------------------.
    # | CONSTRUCTION, LOADING & SAVING                                     |
    # '--------------------------------------------------------------------'

    def __init__(self, folder, host_name, attributes, cluster_nodes):
        super(CREHost, self).__init__()
        self._folder = folder
        self._name = host_name
        self._attributes = attributes
        self._cluster_nodes = cluster_nodes
        self._cached_host_tags = None

    def __repr__(self):
        return "Host(%r)" % (self._name)

    def drop_caches(self):
        super(CREHost, self).drop_caches()
        self._cached_host_tags = None

    # .--------------------------------------------------------------------.
    # | ELEMENT ACCESS                                                     |
    # '--------------------------------------------------------------------'

    def ident(self):
        return self.name()

    def name(self):
        return self._name

    def alias(self):
        # Alias cannot be inherited, so no need to use effective_attributes()
        return self.attributes().get("alias")

    def folder(self):
        return self._folder

    def linkinfo(self):
        return self.folder().path() + ":" + self.name()

    def locked(self):
        return self.folder().locked_hosts()

    def need_unlocked(self):
        return self.folder().need_unlocked_hosts()

    def is_cluster(self):
        return self._cluster_nodes is not None

    def cluster_nodes(self):
        return self._cluster_nodes

    def is_offline(self):
        return self.tag("criticality") == "offline"

    def site_id(self):
        return self._attributes.get("site") or self.folder().site_id()

    def parents(self):
        return self.effective_attribute("parents", [])

    def tag_groups(self):
        # type: () -> dict
        """Compute tags from host attributes
        Each tag attribute may set multiple tags.  can set tags (e.g. the SiteAttribute)"""

        if self._cached_host_tags is not None:
            return self._cached_host_tags  # Cached :-)

        tag_groups = {}  # type: Dict[str, str]
        effective = self.effective_attributes()
        for attr in host_attribute_registry.attributes():
            value = effective.get(attr.name())
            tag_groups.update(attr.get_tag_groups(value))

        # When a host as been configured not to use the agent and not to use
        # SNMP, it needs to get the ping tag assigned.
        # Because we need information from multiple attributes to get this
        # information, we need to add this decision here.
        # Skip this in case no-ip is configured: A ping check is useless in this case
        if tag_groups["snmp_ds"] == "no-snmp" \
           and tag_groups["agent"] == "no-agent" \
           and tag_groups["address_family"] != "no-ip":
            tag_groups["ping"] = "ping"

        # The following code is needed to migrate host/rule matching from <1.5
        # to 1.5 when a user did not modify the "agent type" tag group.  (See
        # migrate_old_sample_config_tag_groups() for more information)
        aux_tag_ids = [t.id for t in config.tags.aux_tag_list.get_tags()]

        # Be compatible to: Agent type -> SNMP v2 or v3
        if tag_groups["agent"] == "no-agent" and tag_groups["snmp_ds"] == "snmp-v2" \
           and "snmp-only" in aux_tag_ids:
            tag_groups["snmp-only"] = "snmp-only"

        # Be compatible to: Agent type -> Dual: SNMP + TCP
        if tag_groups["agent"] == "cmk-agent" and tag_groups["snmp_ds"] == "snmp-v2" \
           and "snmp-tcp" in aux_tag_ids:
            tag_groups["snmp-tcp"] = "snmp-tcp"

        self._cached_host_tags = tag_groups
        return tag_groups

    # TODO: Can we remove this?
    def tags(self):
        # The pre 1.6 tags contained only the tag group values (-> chosen tag id),
        # but there was a single tag group added with it's leading tag group id. This
        # was the internal "site" tag that is created by HostAttributeSite.
        tags = set(v for k, v in self.tag_groups().items() if k != "site")
        tags.add("site:%s" % self.tag_groups()["site"])
        return tags

    def is_ping_host(self):
        return self.tag_groups().get("ping") == "ping"

    def tag(self, taggroup_name):
        effective = self.effective_attributes()
        attribute_name = "tag_" + taggroup_name
        return effective.get(attribute_name)

    def discovery_failed(self):
        return self.attributes().get("inventory_failed", False)

    def validation_errors(self):
        if hooks.registered('validate-host'):
            errors = []
            for hook in hooks.get('validate-host'):
                try:
                    hook.handler(self)
                except MKUserError as e:
                    errors.append("%s" % e)
            return errors
        return []

    def effective_attributes(self):
        try:
            return self._get_cached_effective_attributes()  # cached :-)
        except KeyError:
            pass

        effective = self.folder().effective_attributes()
        effective.update(self.attributes())
        self._cache_effective_attributes(effective)
        return effective

    def labels(self):
        """Returns the aggregated labels for the current host

        The labels of all parent folders and the host are added together. When multiple
        objects define the same tag group, the nearest to the host wins."""
        labels = {}
        for obj in self.folder().parent_folder_chain() + [self.folder(), self]:
            labels.update(obj.attributes().get("labels", {}).items())
        return labels

    def groups(self):
        return self.folder().groups(self)

    def _user_needs_permission(self, how):
        if how == "write" and config.user.may("wato.all_folders"):
            return

        if how == "read" and config.user.may("wato.see_all_folders"):
            return

        if how == "write":
            config.user.need_permission("wato.edit_hosts")

        permitted_groups, _host_contact_groups, _use_for_services = self.groups()
        user_contactgroups = userdb.contactgroups_of_user(config.user.id)

        for c in user_contactgroups:
            if c in permitted_groups:
                return

        reason = _("Sorry, you have no permission on the host '<b>%s</b>'. The host's contact "
                   "groups are <b>%s</b>, your contact groups are <b>%s</b>.") % \
                   (self.name(), ", ".join(permitted_groups), ", ".join(user_contactgroups))
        raise MKAuthException(reason)

    def edit_url(self):
        return html.makeuri_contextless([
            ("mode", "edit_host"),
            ("folder", self.folder().path()),
            ("host", self.name()),
        ])

    def params_url(self):
        return html.makeuri_contextless([
            ("mode", "object_parameters"),
            ("folder", self.folder().path()),
            ("host", self.name()),
        ])

    def services_url(self):
        return html.makeuri_contextless([
            ("mode", "inventory"),
            ("folder", self.folder().path()),
            ("host", self.name()),
        ])

    def clone_url(self):
        return html.makeuri_contextless([
            ("mode", "newcluster" if self.is_cluster() else "newhost"),
            ("folder", self.folder().path()),
            ("clone", self.name()),
        ])

    # .--------------------------------------------------------------------.
    # | MODIFICATIONS                                                      |
    # |                                                                    |
    # | These methods are for being called by actual WATO modules when they|
    # | want to modify hosts. See details at the comment header in Folder. |
    # '--------------------------------------------------------------------'

    def edit(self, attributes, cluster_nodes):
        # 1. Check preconditions
        if attributes.get("contactgroups") != self._attributes.get("contactgroups"):
            self._need_folder_write_permissions()
        self.need_permission("write")
        self.need_unlocked()
        must_be_in_contactgroups(attributes.get("contactgroups"))

        # 2. Actual modification
        affected_sites = [self.site_id()]
        self._attributes = attributes
        self._cluster_nodes = cluster_nodes
        affected_sites = list(set(affected_sites + [self.site_id()]))
        self.folder().save_hosts()
        add_change(
            "edit-host", _("Modified host %s.") % self.name(), obj=self, sites=affected_sites)

    def update_attributes(self, changed_attributes):
        new_attributes = self.attributes().copy()
        new_attributes.update(changed_attributes)
        self.edit(new_attributes, self._cluster_nodes)

    def clean_attributes(self, attrnames_to_clean):
        # 1. Check preconditions
        if "contactgroups" in attrnames_to_clean:
            self._need_folder_write_permissions()
        self.need_unlocked()

        # 2. Actual modification
        affected_sites = [self.site_id()]
        for attrname in attrnames_to_clean:
            if attrname in self._attributes:
                del self._attributes[attrname]
        affected_sites = list(set(affected_sites + [self.site_id()]))
        self.folder().save_hosts()
        add_change(
            "edit-host",
            _("Removed explicit attributes of host %s.") % self.name(),
            obj=self,
            sites=affected_sites)

    def _need_folder_write_permissions(self):
        if not self.folder().may("write"):
            raise MKAuthException(
                _("Sorry. In order to change the permissions of a host you need write "
                  "access to the folder it is contained in."))

    def clear_discovery_failed(self):
        # 1. Check preconditions
        # We do not check permissions. They are checked during the discovery.
        self.need_unlocked()

        # 2. Actual modification
        self.set_discovery_failed(False)

    def set_discovery_failed(self, how=True):
        # 1. Check preconditions
        # We do not check permissions. They are checked during the discovery.
        self.need_unlocked()

        # 2. Actual modification
        if how:
            if not self._attributes.get("inventory_failed"):
                self._attributes["inventory_failed"] = True
                self.folder().save_hosts()
        else:
            if self._attributes.get("inventory_failed"):
                del self._attributes["inventory_failed"]
                self.folder().save_hosts()

    def rename_cluster_node(self, oldname, newname):
        # We must not check permissions here. Permissions
        # on the renamed host must be sufficient. If we would
        # fail here we would leave an inconsistent state
        changed = rename_host_in_list(self._cluster_nodes, oldname, newname)
        if not changed:
            return False

        add_change(
            "rename-node",
            _("Renamed cluster node from %s into %s.") % (oldname, newname),
            obj=self,
            sites=[self.site_id()])
        self.folder().save_hosts()
        return True

    def rename_parent(self, oldname, newname):
        # Same is with rename_cluster_node()
        changed = rename_host_in_list(self._attributes["parents"], oldname, newname)
        if not changed:
            return False

        add_change(
            "rename-parent",
            _("Renamed parent from %s into %s.") % (oldname, newname),
            obj=self,
            sites=[self.site_id()])
        self.folder().save_hosts()
        return True

    def rename(self, new_name):
        add_change(
            "rename-host",
            _("Renamed host from %s into %s.") % (self.name(), new_name),
            obj=self,
            sites=[self.site_id()])
        self._name = new_name


# Make sure that the user is in all of cgs contact groups.
# This is needed when the user assigns contact groups to
# objects. He may only assign such groups he is member himself.
def must_be_in_contactgroups(cgspec):
    if config.user.may("wato.all_folders"):
        return

    # No contact groups specified
    if cgspec is None:
        return

    cgconf = convert_cgroups_from_tuple(cgspec)
    cgs = cgconf["groups"]
    users = userdb.load_users()
    if config.user.id not in users:
        user_cgs = []
    else:
        user_cgs = users[config.user.id]["contactgroups"]
    for c in cgs:
        if c not in user_cgs:
            raise MKAuthException(
                _("Sorry, you cannot assign the contact group '<b>%s</b>' "
                  "because you are not member in that group. Your groups are: <b>%s</b>") %
                (c, ", ".join(user_cgs)))


#.
#   .--CME-----------------------------------------------------------------.
#   |                          ____ __  __ _____                           |
#   |                         / ___|  \/  | ____|                          |
#   |                        | |   | |\/| |  _|                            |
#   |                        | |___| |  | | |___                           |
#   |                         \____|_|  |_|_____|                          |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   | Managed Services Edition specific things                             |
#   '----------------------------------------------------------------------'
# TODO: This has been moved directly into watolib because it was not easily possible
# to extract Folder/Host dependencies to a separate module. As soon as we have untied
# this we should re-establish a watolib plugin hierarchy and move this to a CME
# specific watolib plugin


class CMEFolder(CREFolder):
    def edit(self, new_title, new_attributes):
        if "site" in new_attributes:
            site_id = new_attributes["site"]
            if not self.is_root():
                self.parent()._check_parent_customer_conflicts(site_id)
            self._check_childs_customer_conflicts(site_id)

        super(CMEFolder, self).edit(new_title, new_attributes)

    def _check_parent_customer_conflicts(self, site_id):
        new_customer_id = managed.get_customer_of_site(site_id)
        customer_id = self._get_customer_id()

        if new_customer_id == managed.default_customer_id() and\
           customer_id     != managed.default_customer_id():
            raise MKUserError(
                None,
                _("The configured target site refers to the default customer <i>%s</i>. The parent folder however, "
                  "already have the specific customer <i>%s</i> set. This violates the CME folder hierarchy."
                 ) % (managed.get_customer_name_by_id(managed.default_customer_id()),
                      managed.get_customer_name_by_id(customer_id)))

        # The parents customer id may be the default customer or the same customer
        customer_id = self._get_customer_id()
        if customer_id not in [managed.default_customer_id(), new_customer_id]:
            folder_sites = ", ".join(managed.get_sites_of_customer(customer_id))
            raise MKUserError(None, _("The configured target site <i>%s</i> for this folder is invalid. The folder <i>%s</i> already belongs "
                                      "to the customer <i>%s</i>. This violates the CME folder hierarchy. You may choose the "\
                                      "following sites <i>%s</i>.") % (config.allsites()[site_id]["alias"],
                                                                       self.title(),
                                                                       managed.get_customer_name_by_id(customer_id),
                                                                       folder_sites))

    def _check_childs_customer_conflicts(self, site_id):
        customer_id = managed.get_customer_of_site(site_id)
        # Check hosts
        self._check_hosts_customer_conflicts(site_id)

        # Check subfolders
        for subfolder in self.all_subfolders().values():
            subfolder_explicit_site = subfolder.attributes().get("site")
            if subfolder_explicit_site:
                subfolder_customer = subfolder._get_customer_id()
                if subfolder_customer != customer_id:
                    raise MKUserError(None, _("The subfolder <i>%s</i> has the explicit site <i>%s</i> set, which belongs to "
                                              "customer <i>%s</i>. This violates the CME folder hierarchy.") %\
                                              (subfolder.title(),
                                               config.allsites()[subfolder_explicit_site]["alias"],
                                               managed.get_customer_name_by_id(subfolder_customer)))

            subfolder._check_childs_customer_conflicts(site_id)

    def _check_hosts_customer_conflicts(self, site_id):
        customer_id = managed.get_customer_of_site(site_id)
        for host in self.hosts().values():
            host_explicit_site = host.attributes().get("site")
            if host_explicit_site:
                host_customer = managed.get_customer_of_site(host_explicit_site)
                if host_customer != customer_id:
                    raise MKUserError(None, _("The host <i>%s</i> has the explicit site <i>%s</i> set, which belongs to "
                                              "customer <i>%s</i>. This violates the CME folder hierarchy.") %\
                                              (host.name(),
                                               config.allsites()[host_explicit_site]["alias"],
                                               managed.get_customer_name_by_id(host_customer)))

    def create_subfolder(self, name, title, attributes):
        if "site" in attributes:
            self._check_parent_customer_conflicts(attributes["site"])
        return super(CMEFolder, self).create_subfolder(name, title, attributes)

    def move_subfolder_to(self, subfolder, target_folder):
        target_folder_customer = target_folder._get_customer_id()
        if target_folder_customer != managed.default_customer_id():
            result_dict = {
                "explicit_host_sites": {},  # May be used later on to
                "explicit_folder_sites": {},  # improve error message
                "involved_customers": set()
            }
            subfolder._determine_involved_customers(result_dict)
            other_customers = result_dict["involved_customers"] - set([target_folder_customer])
            if other_customers:
                other_customers_text = ", ".join(
                    map(managed.get_customer_name_by_id, other_customers))
                raise MKUserError(
                    None,
                    _("Cannot move folder. Some of its elements have specifically other customers set (<i>%s</i>). "
                      "This violates the CME folder hierarchy.") % other_customers_text)

        # The site attribute is not explicitely set. The new inheritance might brake something..
        super(CMEFolder, self).move_subfolder_to(subfolder, target_folder)

    def create_hosts(self, entries):
        customer_id = self._get_customer_id()
        if customer_id != managed.default_customer_id():
            for hostname, attributes, _cluster_nodes in entries:
                self.check_modify_host(hostname, attributes)

        super(CMEFolder, self).create_hosts(entries)

    def check_modify_host(self, hostname, attributes):
        if "site" not in attributes:
            return

        customer_id = self._get_customer_id()
        if customer_id != managed.default_customer_id():
            host_customer_id = managed.get_customer_of_site(attributes["site"])
            if host_customer_id != customer_id:
                folder_sites = ", ".join(managed.get_sites_of_customer(customer_id))
                raise MKUserError(
                    None,
                    _("Unable to modify host <i>%s</i>. Its site id <i>%s</i> conflicts with the customer <i>%s</i>, "
                      "which owns this folder. This violates the CME folder hierarchy. You may "
                      "choose the sites: %s") %
                    (hostname, config.allsites()[attributes["site"]]["alias"], customer_id,
                     folder_sites))

    def move_hosts(self, host_names, target_folder):
        # Check if the target folder may have this host
        # A host from customerA is not allowed in a customerB folder
        target_site_id = target_folder.site_id()

        # Check if the hosts are moved to a provider folder
        target_customer_id = managed.get_customer_of_site(target_site_id)
        if target_customer_id != managed.default_customer_id():
            allowed_sites = managed.get_sites_of_customer(target_customer_id)
            for hostname in host_names:
                host = self.host(hostname)
                host_site = host.attributes().get("site")
                if not host_site:
                    continue
                if host_site not in allowed_sites:
                    raise MKUserError(None, _("Unable to move host <i>%s</i>. Its explicit set site attribute <i>%s</i> "\
                                              "belongs to customer <i>%s</i>. The target folder however, belongs to customer <i>%s</i>. "\
                                              "This violates the folder CME folder hierarchy.") % \
                                              (hostname, config.allsites()[host_site]["alias"],
                                                managed.get_customer_of_site(host_site),
                                                managed.get_customer_of_site(target_site_id)))

        super(CMEFolder, self).move_hosts(host_names, target_folder)

    def _get_customer_id(self):
        customer_id = managed.get_customer_of_site(self.site_id())
        return customer_id

    def _determine_involved_customers(self, result_dict):
        self._determine_explicit_set_site_ids(result_dict)
        result_dict["involved_customers"].update(
            set(map(managed.get_customer_of_site, result_dict["explicit_host_sites"].keys())))
        result_dict["involved_customers"].update(
            map(managed.get_customer_of_site, result_dict["explicit_folder_sites"].keys()))

    def _determine_explicit_set_site_ids(self, result_dict):
        for host in self.hosts().values():
            host_explicit_site = host.attributes().get("site")
            if host_explicit_site:
                result_dict["explicit_host_sites"].setdefault(host_explicit_site,
                                                              []).append(host.name())

        for subfolder in self.all_subfolders().values():
            subfolder_explicit_site = subfolder.attributes().get("site")
            if subfolder_explicit_site:
                result_dict["explicit_folder_sites"].setdefault(subfolder_explicit_site,
                                                                []).append(subfolder.title())
            subfolder._determine_explicit_set_site_ids(result_dict)

        return result_dict


class CMEHost(CREHost):
    def edit(self, attributes, cluster_nodes):
        self.folder().check_modify_host(self.name(), attributes)
        super(CMEHost, self).edit(attributes, cluster_nodes)


if cmk.is_managed_edition():
    Folder = CMEFolder
    Host = CMEHost
else:
    Folder = CREFolder
    Host = CREHost


def call_hook_hosts_changed(folder):
    if hooks.registered("hosts-changed"):
        hosts = collect_hosts(folder)
        hooks.call("hosts-changed", hosts)

    # The same with all hosts!
    if hooks.registered("all-hosts-changed"):
        hosts = collect_hosts(Folder.root_folder())
        hooks.call("all-hosts-changed", hosts)


# This hook is called in order to determine the errors of the given
# hostnames. These informations are used for displaying warning
# symbols in the host list and the host detail view
# Returns dictionary { hostname: [errors] }
def validate_all_hosts(hostnames, force_all=False):
    if hooks.registered('validate-all-hosts') and (len(hostnames) > 0 or force_all):
        hosts_errors = {}
        all_hosts = collect_hosts(Folder.root_folder())

        if force_all:
            hostnames = all_hosts.keys()

        for name in hostnames:
            eff = all_hosts[name]
            errors = []
            for hook in hooks.get('validate-all-hosts'):
                try:
                    hook.handler(eff, all_hosts)
                except MKUserError as e:
                    errors.append("%s" % e)
            hosts_errors[name] = errors
        return hosts_errors
    else:
        return {}


def collect_all_hosts():
    return collect_hosts(Folder.root_folder())


def collect_hosts(folder):
    hosts_attributes = {}
    for host_name, host in Host.all().items():
        hosts_attributes[host_name] = host.effective_attributes()
        hosts_attributes[host_name]["path"] = host.folder().path()
    return hosts_attributes


def folder_preserving_link(add_vars):
    return Folder.current().url(add_vars)


def make_action_link(vars_):
    return folder_preserving_link(vars_ + [("_transid", html.transaction_manager.get())])


def get_folder_title_path(path, with_links=False):
    """Return a list with all the titles of the paths'
    components, e.g. "muc/north" -> [ "Main Directory", "Munich", "North" ]"""
    # In order to speed this up, we work with a per HTML-request cache
    cache_name = "wato_folder_titles" + (with_links and "_linked" or "")
    cache = current_app.g.setdefault(cache_name, {})
    if path not in cache:
        cache[path] = Folder.folder(path).title_path(with_links)
    return cache[path]


def get_folder_title(path):
    """Return the title of a folder - which is given as a string path"""
    folder = Folder.folder(path)
    if folder:
        return folder.title()
    return path


# TODO: Move to Folder()?
def check_wato_foldername(htmlvarname, name, just_name=False):
    if not just_name and Folder.current().has_subfolder(name):
        raise MKUserError(htmlvarname, _("A folder with that name already exists."))

    if not name:
        raise MKUserError(htmlvarname, _("Please specify a name."))

    if not re.match("^[-a-z0-9A-Z_]*$", name):
        raise MKUserError(
            htmlvarname,
            _("Invalid folder name. Only the characters a-z, A-Z, 0-9, _ and - are allowed."))


def get_meta_data(created_by):
    return {
        "created_at": time.time(),
        "created_by": created_by,
    }
