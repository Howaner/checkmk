#!/usr/bin/env python
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
"""Modes for renaming one or multiple existing hosts"""

import os
import socket

from cmk.utils.regex import regex
import cmk.utils.store as store

import cmk.gui.pages
import cmk.gui.config as config
import cmk.gui.watolib as watolib
import cmk.gui.userdb as userdb
import cmk.gui.forms as forms
import cmk.gui.background_job as background_job
import cmk.gui.gui_background_job as gui_background_job
from cmk.gui.htmllib import HTML
from cmk.gui.exceptions import HTTPRedirect, MKUserError, MKGeneralException, MKAuthException
from cmk.gui.i18n import _
from cmk.gui.globals import html
from cmk.gui.watolib.hosts_and_folders import validate_host_uniqueness
from cmk.gui.watolib.notifications import (
    load_notification_rules,
    save_notification_rules,
)

from cmk.gui.valuespec import (
    Hostname,
    Tuple,
    TextUnicode,
    RegExpUnicode,
    DropdownChoice,
    RegExp,
    ListOf,
    Checkbox,
    CascadingDropdown,
    Dictionary,
)

from cmk.gui.plugins.wato.utils import WatoBackgroundJob
from cmk.gui.plugins.wato import (
    WatoMode,
    mode_registry,
    global_buttons,
    add_change,
    wato_confirm,
)


@gui_background_job.job_registry.register
class RenameHostsBackgroundJob(WatoBackgroundJob):
    job_prefix = "rename-hosts"

    @classmethod
    def gui_title(cls):
        return _("Host renaming")

    def __init__(self, title=None):
        if not title:
            title = _("Host renaming")

        kwargs = {}
        kwargs["title"] = title
        kwargs["lock_wato"] = True
        kwargs["stoppable"] = False
        last_job_status = WatoBackgroundJob(self.job_prefix).get_status()
        if "duration" in last_job_status:
            kwargs["estimated_duration"] = last_job_status["duration"]

        super(RenameHostsBackgroundJob, self).__init__(self.job_prefix, **kwargs)

        if self.is_running():
            raise MKGeneralException(_("Another renaming operation is currently in progress"))

    def _back_url(self):
        return html.makeuri([])


@gui_background_job.job_registry.register
class RenameHostBackgroundJob(RenameHostsBackgroundJob):
    def __init__(self, host, title=None):
        super(RenameHostBackgroundJob, self).__init__(title)
        self._host = host

    def _back_url(self):
        return self._host.folder().url()


@mode_registry.register
class ModeBulkRenameHost(WatoMode):
    @classmethod
    def name(cls):
        return "bulk_rename_host"

    @classmethod
    def permissions(cls):
        return ["hosts", "manage_hosts"]

    def __init__(self):
        super(ModeBulkRenameHost, self).__init__()

        if not config.user.may("wato.rename_hosts"):
            raise MKGeneralException(_("You don't have the right to rename hosts"))

    def title(self):
        return _("Bulk renaming of hosts")

    def buttons(self):
        html.context_button(
            _("Folder"), watolib.folder_preserving_link([("mode", "folder")]), "back")
        host_renaming_job = RenameHostsBackgroundJob()
        if host_renaming_job.is_available():
            html.context_button(
                _("Last result"), host_renaming_job.detail_url(), "background_job_details")

    def action(self):
        renaming_config = self._vs_renaming_config().from_html_vars("")
        self._vs_renaming_config().validate_value(renaming_config, "")
        renamings = self._collect_host_renamings(renaming_config)

        if not renamings:
            return None, _("No matching host names")

        warning = self._renaming_collision_error(renamings)
        if warning:
            return None, warning

        message = _(
            "<b>Do you really want to rename to following hosts? This involves a restart of the monitoring core!</b>"
        )
        message += "<table>"
        for _folder, host_name, target_name in renamings:
            message += u"<tr><td>%s</td><td> → %s</td></tr>" % (host_name, target_name)
        message += "</table>"

        c = wato_confirm(_("Confirm renaming of %d hosts") % len(renamings), HTML(message))
        if c:
            title = _("Renaming of %s") % ", ".join(u"%s → %s" % x[1:] for x in renamings)
            host_renaming_job = RenameHostsBackgroundJob(title=title)
            host_renaming_job.set_function(rename_hosts_background_job, renamings)

            try:
                host_renaming_job.start()
            except background_job.BackgroundJobAlreadyRunning as e:
                raise MKGeneralException(_("Another host renaming job is already running: %s") % e)

            raise HTTPRedirect(host_renaming_job.detail_url())
        elif c is False:  # not yet confirmed
            return ""
        else:
            return None  # browser reload

    def _renaming_collision_error(self, renamings):
        name_collisions = set()
        new_names = [new_name for _folder, _old_name, new_name in renamings]
        all_host_names = watolib.Host.all().keys()
        for name in new_names:
            if name in all_host_names:
                name_collisions.add(name)
        for name in new_names:
            if new_names.count(name) > 1:
                name_collisions.add(name)

        if name_collisions:
            warning = "<b>%s</b><ul>" % _(
                "You cannot do this renaming since the following host names would collide:")
            for name in sorted(list(name_collisions)):
                warning += "<li>%s</li>" % name
            warning += "</ul>"
            return warning

    def _collect_host_renamings(self, renaming_config):
        return self._recurse_hosts_for_renaming(watolib.Folder.current(), renaming_config)

    def _recurse_hosts_for_renaming(self, folder, renaming_config):
        entries = []
        for host_name, host in folder.hosts().items():
            target_name = self._host_renamed_into(host_name, renaming_config)
            if target_name and host.may("write"):
                entries.append((folder, host_name, target_name))
        if renaming_config["recurse"]:
            for subfolder in folder.all_subfolders().values():
                entries += self._recurse_hosts_for_renaming(subfolder, renaming_config)
        return entries

    def _host_renamed_into(self, hostname, renaming_config):
        prefix_regex = regex(renaming_config["match_hostname"])
        if not prefix_regex.match(hostname):
            return None

        new_hostname = hostname
        for operation in renaming_config["renamings"]:
            new_hostname = self._host_renaming_operation(operation, new_hostname)

        if new_hostname != hostname:
            return new_hostname
        return None

    def _host_renaming_operation(self, operation, hostname):
        if operation == "drop_domain":
            return hostname.split(".", 1)[0]
        elif operation == "reverse_dns":
            try:
                reverse_dns = socket.gethostbyaddr(hostname)[0]
                return reverse_dns
            except:
                return hostname

        elif operation == ('case', 'upper'):
            return hostname.upper()
        elif operation == ('case', 'lower'):
            return hostname.lower()
        elif operation[0] == 'add_suffix':
            return hostname + operation[1]
        elif operation[0] == 'add_prefix':
            return operation[1] + hostname
        elif operation[0] == 'explicit':
            old_name, new_name = operation[1]
            if old_name == hostname:
                return new_name
            return hostname
        elif operation[0] == 'regex':
            match_regex, new_name = operation[1]
            match = regex(match_regex).match(hostname)
            if match:
                for nr, group in enumerate(match.groups()):
                    new_name = new_name.replace("\\%d" % (nr + 1), group)
                new_name = new_name.replace("\\0", hostname)
                return new_name
            return hostname

    def page(self):
        html.begin_form("bulk_rename_host", method="POST")
        self._vs_renaming_config().render_input("", {})
        html.button("_start", _("Bulk Rename"))
        html.hidden_fields()
        html.end_form()

    def _vs_renaming_config(self):
        return Dictionary(
            title=_("Bulk Renaming"),
            render="form",
            elements=[
                ("recurse",
                 Checkbox(
                     title=_("Folder Selection"),
                     label=_("Include all subfolders"),
                     default_value=True,
                 )),
                ("match_hostname",
                 RegExp(
                     title=_("Hostname matching"),
                     help=
                     _("Only rename hostnames whose names <i>begin</i> with the regular expression entered here."
                      ),
                     mode=RegExp.complete,
                 )),
                ("renamings",
                 ListOf(
                     self._vs_host_renaming(),
                     title=_("Renaming Operations"),
                     add_label=_("Add renaming"),
                     allow_empty=False,
                 )),
            ],
            optional_keys=[],
        )

    def _vs_host_renaming(self):
        return CascadingDropdown(
            orientation="horizontal",
            choices=[
                ("case", _("Case translation"),
                 DropdownChoice(choices=[
                     ("upper", _("Convert hostnames to upper case")),
                     ("lower", _("Convert hostnames to lower case")),
                 ])),
                ("add_suffix", _("Add Suffix"), Hostname()),
                ("add_prefix", _("Add Prefix"), Hostname()),
                ("drop_domain", _("Drop Domain Suffix")),
                ("reverse_dns", _("Convert IP addresses of hosts into host their DNS names")),
                ("regex", _("Regular expression substitution"),
                 Tuple(
                     help=
                     _("Please specify a regular expression in the first field. This expression should at "
                       "least contain one subexpression exclosed in brackets - for example <tt>vm_(.*)_prod</tt>. "
                       "In the second field you specify the translated host name and can refer to the first matched "
                       "group with <tt>\\1</tt>, the second with <tt>\\2</tt> and so on, for example <tt>\\1.example.org</tt>"
                      ),
                     elements=[
                         RegExpUnicode(
                             title=_("Regular expression for the beginning of the host name"),
                             help=_("Must contain at least one subgroup <tt>(...)</tt>"),
                             mingroups=0,
                             maxgroups=9,
                             size=30,
                             allow_empty=False,
                             mode=RegExpUnicode.prefix,
                         ),
                         TextUnicode(
                             title=_("Replacement"),
                             help=
                             _("Use <tt>\\1</tt>, <tt>\\2</tt> etc. to replace matched subgroups, <tt>\\0</tt> to insert to original host name"
                              ),
                             size=30,
                             allow_empty=False,
                         )
                     ])),
                ("explicit", _("Explicit renaming"),
                 Tuple(
                     orientation="horizontal",
                     elements=[
                         Hostname(title=_("current host name"), allow_empty=False),
                         Hostname(title=_("new host name"), allow_empty=False),
                     ])),
            ])


def rename_hosts_background_job(renamings, job_interface=None):
    actions, auth_problems = rename_hosts(
        renamings, job_interface=job_interface)  # Already activates the changes!
    watolib.confirm_all_local_changes()  # All activated by the underlying rename automation
    action_txt = "".join(["<li>%s</li>" % a for a in actions])
    message = _("Renamed %d hosts at the following places:<br><ul>%s</ul>") % (len(renamings),
                                                                               action_txt)
    if auth_problems:
        message += _("The following hosts could not be renamed because of missing permissions: %s"
                    ) % ", ".join(
                        ["%s (%s)" % (host_name, reason) for (host_name, reason) in auth_problems])
    job_interface.send_result_message(message)


@mode_registry.register
class ModeRenameHost(WatoMode):
    @classmethod
    def name(cls):
        return "rename_host"

    @classmethod
    def permissions(cls):
        return ["hosts", "manage_hosts"]

    def _from_vars(self):
        host_name = html.get_ascii_input("host")

        if not watolib.Folder.current().has_host(host_name):
            raise MKUserError("host", _("You called this page with an invalid host name."))

        if not config.user.may("wato.rename_hosts"):
            raise MKAuthException(_("You don't have the right to rename hosts"))

        self._host = watolib.Folder.current().host(host_name)
        self._host.need_permission("write")

    def title(self):
        return _("Rename %s %s") % (_("Cluster") if self._host.is_cluster() else _("Host"),
                                    self._host.name())

    def buttons(self):
        global_buttons()
        html.context_button(_("Host Properties"), self._host.edit_url(), "back")

        host_renaming_job = RenameHostBackgroundJob(self._host)
        if host_renaming_job.is_available():
            html.context_button(
                _("Last result"), host_renaming_job.detail_url(), "background_job_details")

    def action(self):
        if watolib.get_pending_changes_info():
            raise MKUserError("newname",
                              _("You cannot rename a host while you have pending changes."))

        newname = html.request.var("newname")
        self._check_new_host_name("newname", newname)
        c = wato_confirm(
            _("Confirm renaming of host"),
            _("Are you sure you want to rename the host <b>%s</b> into <b>%s</b>? "
              "This involves a restart of the monitoring core!") % (self._host.name(), newname))
        if c:
            # Creating pending entry. That makes the site dirty and that will force a sync of
            # the config to that site before the automation is being done.
            host_renaming_job = RenameHostBackgroundJob(
                self._host, title=_("Renaming of %s -> %s") % (self._host.name(), newname))
            renamings = [(watolib.Folder.current(), self._host.name(), newname)]
            host_renaming_job.set_function(rename_hosts_background_job, renamings)

            try:
                host_renaming_job.start()
            except background_job.BackgroundJobAlreadyRunning as e:
                raise MKGeneralException(_("Another host renaming job is already running: %s") % e)

            raise HTTPRedirect(host_renaming_job.detail_url())

        elif c is False:  # not yet confirmed
            return ""

    def _check_new_host_name(self, varname, host_name):
        if not host_name:
            raise MKUserError(varname, _("Please specify a host name."))
        elif watolib.Folder.current().has_host(host_name):
            raise MKUserError(varname, _("A host with this name already exists in this folder."))
        validate_host_uniqueness(varname, host_name)
        Hostname().validate_value(host_name, varname)

    def page(self):
        html.help(
            _("The renaming of hosts is a complex operation since a host's name is being "
              "used as a unique key in various places. It also involves stopping and starting "
              "of the monitoring core. You cannot rename a host while you have pending changes."))

        html.begin_form("rename_host", method="POST")
        forms.header(_("Rename host %s") % self._host.name())
        forms.section(_("Current name"))
        html.write_text(self._host.name())
        forms.section(_("New name"))
        html.text_input("newname", "")
        forms.end()
        html.set_focus("newname")
        html.button("rename", _("Rename host!"), "submit")
        html.hidden_fields()
        html.end_form()


def rename_host_in_folder(folder, oldname, newname):
    folder.rename_host(oldname, newname)
    return ["folder"]


def rename_host_as_cluster_node(all_hosts, oldname, newname):
    clusters = []
    for somehost in all_hosts.values():
        if somehost.is_cluster():
            if somehost.rename_cluster_node(oldname, newname):
                clusters.append(somehost.name())
    if clusters:
        return ["cluster_nodes"] * len(clusters)
    return []


def rename_host_in_parents(oldname, newname):
    parents = rename_host_as_parent(oldname, newname)
    return ["parents"] * len(parents)


def rename_host_as_parent(oldname, newname, in_folder=None):
    if in_folder is None:
        in_folder = watolib.Folder.root_folder()

    parents = []
    for somehost in in_folder.hosts().values():
        if somehost.has_explicit_attribute("parents"):
            if somehost.rename_parent(oldname, newname):
                parents.append(somehost.name())

    if in_folder.has_explicit_attribute("parents"):
        if in_folder.rename_parent(oldname, newname):
            parents.append(in_folder.name())

    for subfolder in in_folder.all_subfolders().values():
        parents += rename_host_as_parent(oldname, newname, subfolder)

    return parents


def rename_host_in_rulesets(folder, oldname, newname):
    # Rules that explicitely name that host (no regexes)
    changed_rulesets = []

    def rename_host_in_folder_rules(folder):
        rulesets = watolib.FolderRulesets(folder)
        rulesets.load()

        changed = False
        for varname, ruleset in rulesets.get_rulesets().items():
            for _rule_folder, _rulenr, rule in ruleset.get_rules():
                if rule.replace_explicit_host_condition(oldname, newname):
                    changed_rulesets.append(varname)
                    changed = True

        if changed:
            add_change(
                "edit-ruleset",
                _("Renamed host in %d rulesets of folder %s") % (len(changed_rulesets),
                                                                 folder.title),
                obj=folder,
                sites=folder.all_site_ids())
            rulesets.save()

        for subfolder in folder.all_subfolders().values():
            rename_host_in_folder_rules(subfolder)

    rename_host_in_folder_rules(watolib.Folder.root_folder())
    if changed_rulesets:
        actions = []
        unique = set(changed_rulesets)
        for varname in unique:
            actions += ["wato_rules"] * changed_rulesets.count(varname)
        return actions
    return []


def rename_host_in_event_rules(oldname, newname):
    actions = []

    def rename_in_event_rules(rules):
        num_changed = 0
        for rule in rules:
            for key in ["match_hosts", "match_exclude_hosts"]:
                if rule.get(key):
                    if watolib.rename_host_in_list(rule[key], oldname, newname):
                        num_changed += 1
        return num_changed

    users = userdb.load_users(lock=True)
    some_user_changed = False
    for user in users.itervalues():
        if user.get("notification_rules"):
            rules = user["notification_rules"]
            num_changed = rename_in_event_rules(rules)
            if num_changed:
                actions += ["notify_user"] * num_changed
                some_user_changed = True

    rules = load_notification_rules()
    num_changed = rename_in_event_rules(rules)
    if num_changed:
        actions += ["notify_global"] * num_changed
        save_notification_rules(rules)

    try:
        import cmk.gui.cee.plugins.wato.alert_handling as alert_handling
    except:
        alert_handling = None

    if alert_handling:
        rules = alert_handling.load_alert_handler_rules()
        if rules:
            num_changed = rename_in_event_rules(rules)
            if num_changed:
                actions += ["alert_rules"] * num_changed
                alert_handling.save_alert_handler_rules(rules)

    # Notification channels of flexible notifications also can have host conditions
    for user in users.itervalues():
        method = user.get("notification_method")
        if method and isinstance(method, tuple) and method[0] == "flexible":
            channels_changed = 0
            for channel in method[1]:
                if channel.get("only_hosts"):
                    num_changed = watolib.rename_host_in_list(channel["only_hosts"], oldname,
                                                              newname)
                    if num_changed:
                        channels_changed += 1
                        some_user_changed = True
            if channels_changed:
                actions += ["notify_flexible"] * channels_changed

    if some_user_changed:
        userdb.save_users(users)

    return actions


def rename_host_in_multisite(oldname, newname):
    # State of Multisite ---------------------------------------
    # Favorites of users and maybe other settings. We simply walk through
    # all directories rather then through the user database. That way we
    # are sure that also currently non-existant users are being found and
    # also only users that really have a profile.
    users_changed = 0
    total_changed = 0
    for userid in os.listdir(config.config_dir):
        if userid[0] == '.':
            continue
        if not os.path.isdir(config.config_dir + "/" + userid):
            continue

        favpath = config.config_dir + "/" + userid + "/favorites.mk"
        num_changed = 0
        favorites = store.load_data_from_file(favpath, [], lock=True)
        for nr, entry in enumerate(favorites):
            if entry == oldname:
                favorites[nr] = newname
                num_changed += 1
            elif entry.startswith(oldname + ";"):
                favorites[nr] = newname + ";" + entry.split(";")[1]
                num_changed += 1

        if num_changed:
            store.save_data_to_file(favpath, favorites)
            users_changed += 1
            total_changed += num_changed
        store.release_lock(favpath)

    if users_changed:
        return ["favorites"] * total_changed
    return []


def rename_host_in_bi(oldname, newname):
    return cmk.gui.plugins.wato.bi.BIHostRenamer().rename_host(oldname, newname)


def rename_hosts_in_check_mk(renamings):
    action_counts = {}
    for site_id, name_pairs in group_renamings_by_site(renamings).items():
        message = _("Renamed host %s") % ", ".join(
            [_("%s into %s") % (oldname, newname) for (oldname, newname) in name_pairs])

        # Restart is done by remote automation (below), so don't do it during rename/sync
        # The sync is automatically done by the remote automation call
        add_change("renamed-hosts", message, sites=[site_id], need_restart=False)

        new_counts = watolib.check_mk_automation(site_id, "rename-hosts", [], name_pairs)

        merge_action_counts(action_counts, new_counts)
    return action_counts


def merge_action_counts(action_counts, new_counts):
    for key, count in new_counts.items():
        action_counts.setdefault(key, 0)
        action_counts[key] += count


def group_renamings_by_site(renamings):
    renamings_per_site = {}
    for folder, oldname, newname in renamings:
        host = folder.host(newname)  # already renamed here!
        site_id = host.site_id()
        renamings_per_site.setdefault(site_id, []).append((oldname, newname))
    return renamings_per_site


# renamings is a list of tuples of (folder, oldname, newname)
def rename_hosts(renamings, job_interface=None):
    actions = []
    all_hosts = watolib.Host.all()

    # 1. Fix WATO configuration itself ----------------
    auth_problems = []
    successful_renamings = []
    job_interface.send_progress_update(_("Renaming WATO configuration..."))
    for folder, oldname, newname in renamings:
        try:
            this_host_actions = []
            job_interface.send_progress_update(_("Renaming host(s) in folders..."))
            this_host_actions += rename_host_in_folder(folder, oldname, newname)
            job_interface.send_progress_update(_("Renaming host(s) in cluster nodes..."))
            this_host_actions += rename_host_as_cluster_node(all_hosts, oldname, newname)
            job_interface.send_progress_update(_("Renaming host(s) in parents..."))
            this_host_actions += rename_host_in_parents(oldname, newname)
            job_interface.send_progress_update(_("Renaming host(s) in rulesets..."))
            this_host_actions += rename_host_in_rulesets(folder, oldname, newname)
            job_interface.send_progress_update(_("Renaming host(s) in BI aggregations..."))
            this_host_actions += rename_host_in_bi(oldname, newname)
            actions += this_host_actions
            successful_renamings.append((folder, oldname, newname))
        except MKAuthException as e:
            auth_problems.append((oldname, e))

    # 2. Check_MK stuff ------------------------------------------------
    job_interface.send_progress_update(
        _("Renaming host(s) in base configuration, rrd, history files, etc."))
    job_interface.send_progress_update(
        _("This might take some time and involves a core restart..."))
    action_counts = rename_hosts_in_check_mk(successful_renamings)

    # 3. Notification settings ----------------------------------------------
    # Notification rules - both global and users' ones
    job_interface.send_progress_update(_("Renaming host(s) in notification rules..."))
    for folder, oldname, newname in successful_renamings:
        actions += rename_host_in_event_rules(oldname, newname)
        actions += rename_host_in_multisite(oldname, newname)

    for action in actions:
        action_counts.setdefault(action, 0)
        action_counts[action] += 1

    job_interface.send_progress_update(_("Calling final hooks"))
    watolib.call_hook_hosts_changed(watolib.Folder.root_folder())

    action_texts = render_renaming_actions(action_counts)
    return action_texts, auth_problems


def render_renaming_actions(action_counts):
    action_titles = {
        "folder": _("WATO folder"),
        "notify_user": _("Users' notification rule"),
        "notify_global": _("Global notification rule"),
        "notify_flexible": _("Flexible notification rule"),
        "wato_rules": _("Host and service configuration rule"),
        "alert_rules": _("Alert handler rule"),
        "parents": _("Parent definition"),
        "cluster_nodes": _("Cluster node definition"),
        "bi": _("BI rule or aggregation"),
        "favorites": _("Favorite entry of user"),
        "cache": _("Cached output of monitoring agent"),
        "counters": _("File with performance counter"),
        "agent": _("Baked host specific agent"),
        "agent_deployment": _("Agent deployment status"),
        "piggyback-load": _("Piggyback information from other host"),
        "piggyback-pig": _("Piggyback information for other hosts"),
        "autochecks": _("Auto-disovered services of the host"),
        "logwatch": _("Logfile information of logwatch plugin"),
        "snmpwalk": _("A stored SNMP walk"),
        "rrd": _("RRD databases with performance data"),
        "rrdcached": _("RRD updates in journal of RRD Cache"),
        "pnpspool": _("Spool files of PNP4Nagios"),
        "nagvis": _("NagVis map"),
        "history": _("Monitoring history entries (events and availability)"),
        "retention": _("The current monitoring state (including acknowledgements and downtimes)"),
        "inv": _("Recent hardware/software inventory"),
        "invarch": _("History of hardware/software inventory"),
    }

    texts = []
    for what, count in sorted(action_counts.items()):
        if what.startswith("dnsfail-"):
            text = _(
                "<b>WARNING: </b> the IP address lookup of <b>%s</b> has failed. The core has been "
                "started by using the address <tt>0.0.0.0</tt> for the while. "
                "Please update your DNS or configure an IP address for the affected host."
            ) % what.split("-", 1)[1]
        else:
            text = action_titles.get(what, what)

        if count > 1:
            text += _(" (%d times)") % count
        texts.append(text)

    return texts
