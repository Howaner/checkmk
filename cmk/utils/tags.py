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
"""Helper functions for dealing with Check_MK tags"""

import re
import abc
from typing import Dict  # pylint: disable=unused-import

from cmk.gui.i18n import _
from cmk.gui.exceptions import MKUserError


def get_effective_tag_config(tag_config):
    # type: (Dict) -> TagConfig
    # We don't want to access the plain config data structure during GUI code processing
    tags = TagConfig()
    tags.parse_config(tag_config)

    # Merge builtin tags with configured tags. The logic favors the configured tags, even
    # when the user config should not conflict with the builtin tags. This is something
    # which could be left over from pre 1.5 setups.
    tags += BuiltinTagConfig()
    return tags


def transform_pre_16_tags(tag_groups, aux_tags):
    cfg = TagConfig()
    cfg.parse_config((tag_groups, aux_tags))
    return cfg.get_dict_format()


def _parse_legacy_title(title):
    if '/' in title:
        return title.split('/', 1)
    return None, title


def _validate_tag_id(tag_id, varname):
    if not re.match("^[-a-z0-9A-Z_]*$", tag_id):
        raise MKUserError(
            varname, _("Invalid tag ID. Only the characters a-z, A-Z, "
                       "0-9, _ and - are allowed."))


class ABCTag(object):
    __metaclass__ = abc.ABCMeta

    def __init__(self):
        super(ABCTag, self).__init__()
        self._initialize()

    def _initialize(self):
        self.id = None
        self.title = None
        self.topic = None

    def validate(self):
        if not self.id:
            raise MKUserError("tag_id", _("Please specify a tag ID"))

        _validate_tag_id(self.id, "tag_id")

        if not self.title:
            raise MKUserError("title", _("Please supply a title for you auxiliary tag."))

    def parse_config(self, data):
        self._initialize()
        if isinstance(data, dict):
            self._parse_from_dict(data)
        else:
            self._parse_legacy_format(data)

    def _parse_from_dict(self, tag_info):
        self.id = tag_info["id"]
        self.title = tag_info["title"]

    def _parse_legacy_format(self, tag_info):
        self.id, self.title = tag_info[:2]


class AuxTag(ABCTag):
    is_aux_tag = True

    def __init__(self, data=None):
        super(AuxTag, self).__init__()
        if data:
            self.parse_config(data)

    def _parse_from_dict(self, tag_info):
        super(AuxTag, self)._parse_from_dict(tag_info)
        if "topic" in tag_info:
            self.topic = tag_info["topic"]

    def _parse_legacy_format(self, tag_info):
        super(AuxTag, self)._parse_legacy_format(tag_info)
        self.topic, self.title = _parse_legacy_title(self.title)

    def get_dict_format(self):
        response = {"id": self.id, "title": self.title}
        if self.topic:
            response["topic"] = self.topic
        return response

    @property
    def choice_title(self):
        if self.topic:
            return "%s / %s" % (self.topic, self.title)
        return self.title


class AuxTagList(object):
    def __init__(self):
        self._tags = []

    def __iadd__(self, other):
        tag_ids = self.get_tag_ids()
        for aux_tag in other.get_tags():
            if aux_tag.id not in tag_ids:
                self.append(aux_tag)
        return self

    def get_tags(self):
        return self._tags

    def append(self, aux_tag):
        # TODO: Reenable this!
        #if is_builtin_aux_tag(aux_tag.id):
        #    raise MKUserError("tag_id", _("You can not override a builtin auxiliary tag."))
        self._append(aux_tag)

    def _append(self, aux_tag):
        if self.has_aux_tag(aux_tag):
            raise MKUserError("tag_id",
                              _("This tag id does already exist in the list "
                                "of auxiliary tags."))
        self._tags.append(aux_tag)

    def update(self, aux_tag_id, aux_tag):
        for index, tmp_aux_tag in enumerate(self._tags):
            if tmp_aux_tag.id == aux_tag_id:
                self._tags[index] = aux_tag
                return

    def remove(self, aux_tag_id):
        for index, tmp_aux_tag in enumerate(self._tags[:]):
            if tmp_aux_tag.id == aux_tag_id:
                self._tags.pop(index)
                return

    def validate(self):
        seen = set()
        for aux_tag in self._tags:
            aux_tag.validate()
            if aux_tag.id in seen:
                raise MKUserError("tag_id", _("Duplicate tag id in auxilary tags: %s") % aux_tag.id)
            seen.add(aux_tag.id)

    def has_aux_tag(self, aux_tag):
        for tmp_aux_tag in self._tags:
            if aux_tag.id == tmp_aux_tag.id:
                return True
        return False

    def get_aux_tag(self, aux_tag_id):
        for aux_tag in self._tags:
            if aux_tag_id == aux_tag.id:
                return aux_tag
        return

    def get_tag_ids(self):
        return {tag.id for tag in self._tags}

    def get_dict_format(self):
        response = []
        for tag in self._tags:
            response.append(tag.get_dict_format())
        return response

    def get_choices(self):
        return [(aux_tag.id, aux_tag.title) for aux_tag in self._tags]


class GroupedTag(ABCTag):
    is_aux_tag = False

    def __init__(self, group, data=None):
        super(GroupedTag, self).__init__()
        self.group = group
        self.aux_tag_ids = []
        self.parse_config(data)

    def _parse_from_dict(self, tag_info):
        super(GroupedTag, self)._parse_from_dict(tag_info)
        self.aux_tag_ids = tag_info["aux_tags"]

    def _parse_legacy_format(self, tag_info):
        super(GroupedTag, self)._parse_legacy_format(tag_info)

        if len(tag_info) == 3:
            self.aux_tag_ids = tag_info[2]

    def get_dict_format(self):
        return {"id": self.id, "title": self.title, "aux_tags": self.aux_tag_ids}


class TagGroup(object):
    def __init__(self, data=None):
        super(TagGroup, self).__init__()
        self._initialize()

        if data:
            if isinstance(data, dict):
                self._parse_from_dict(data)
            else:  # legacy tuple
                self._parse_legacy_format(data)

    def _initialize(self):
        self.id = None
        self.title = None
        self.topic = None
        self.tags = []

    def _parse_from_dict(self, group_info):
        self._initialize()
        self.id = group_info["id"]
        self.title = group_info["title"]
        self.topic = group_info.get("topic")
        self.tags = [GroupedTag(self, tag) for tag in group_info["tags"]]

    def _parse_legacy_format(self, group_info):
        self._initialize()
        group_id, group_title, tag_list = group_info[:3]

        self.id = group_id
        self.topic, self.title = _parse_legacy_title(group_title)

        for tag in tag_list:
            self.tags.append(GroupedTag(self, tag))

    @property
    def choice_title(self):
        if self.topic:
            return "%s / %s" % (self.topic, self.title)
        return self.title

    @property
    def is_checkbox_tag_group(self):
        return len(self.tags) == 1

    @property
    def default_value(self):
        return self.tags[0].id

    def get_tag(self, tag_id):
        for tag in self.tags:
            if tag_id == tag.id:
                return tag
        return

    def get_tag_ids(self):
        return {tag.id for tag in self.tags}

    def get_dict_format(self):
        response = {"id": self.id, "title": self.title, "tags": []}
        if self.topic:
            response["topic"] = self.topic

        for tag in self.tags:
            response["tags"].append(tag.get_dict_format())

        return response

    def get_tag_choices(self):
        choices = []
        for tag in self.tags:
            choices.append((tag.id, tag.title))
        return choices

    def get_tag_group_config(self, value):
        """Return the set of tag groups which should be set for a host based on the given value"""
        tag_groups = {}

        if value is not None:
            tag_groups[self.id] = value

        # add optional aux tags
        for grouped_tag in self.tags:
            if grouped_tag.id == value:
                tag_groups.update({t: t for t in grouped_tag.aux_tag_ids})

        return tag_groups


class TagConfig(object):
    """Container object encapsulating a whole set of configured
    tag groups with auxiliary tags"""

    def __init__(self):
        super(TagConfig, self).__init__()
        self._initialize()

    def _initialize(self):
        self.tag_groups = []
        self.aux_tag_list = AuxTagList()

    def __iadd__(self, other):
        tg_ids = [tg.id for tg in self.tag_groups]
        for tg in other.tag_groups:
            if tg.id not in tg_ids:
                self.tag_groups.append(tg)

        self.aux_tag_list += other.aux_tag_list
        return self

    def get_topic_choices(self):
        names = set([])
        for tag_group in self.tag_groups:
            topic = tag_group.topic or _('Tags')
            if topic:
                names.add((topic, topic))

        for aux_tag in self.aux_tag_list.get_tags():
            topic = aux_tag.topic or _('Tags')
            if topic:
                names.add((topic, topic))

        return sorted(list(names), key=lambda x: x[1])

    def get_tag_groups_by_topic(self):
        by_topic = {}
        for tag_group in self.tag_groups:
            topic = tag_group.topic or _('Tags')
            by_topic.setdefault(topic, []).append(tag_group)
        return sorted(by_topic.items(), key=lambda x: x[0])

    def tag_group_exists(self, tag_group_id):
        return self.get_tag_group(tag_group_id) is not None

    def get_tag_group(self, tag_group_id):
        for group in self.tag_groups:
            if group.id == tag_group_id:
                return group

    def remove_tag_group(self, tag_group_id):
        group = self.get_tag_group(tag_group_id)
        if group is None:
            return
        self.tag_groups.remove(group)

    def get_tag_group_choices(self):
        return [(tg.id, tg.choice_title) for tg in self.tag_groups]

    # TODO: Clean this up and make call sites directly call the wrapped function
    def get_aux_tags(self):
        return self.aux_tag_list.get_tags()

    def get_aux_tags_by_tag(self):
        aux_tag_map = {}
        for tag_group in self.tag_groups:
            for grouped_tag in tag_group.tags:
                aux_tag_map[grouped_tag.id] = grouped_tag.aux_tag_ids
        return aux_tag_map

    def get_aux_tags_by_topic(self):
        by_topic = {}
        for aux_tag in self.aux_tag_list.get_tags():
            topic = aux_tag.topic or _('Tags')
            by_topic.setdefault(topic, []).append(aux_tag)
        return sorted(by_topic.items(), key=lambda x: x[0])

    def get_tag_ids(self):
        """Returns the raw ids of the grouped tags and the aux tags"""
        response = set()
        for tag_group in self.tag_groups:
            response.update(tag_group.get_tag_ids())

        response.update(self.aux_tag_list.get_tag_ids())
        return response

    def get_tag_ids_with_group_prefix(self):
        response = set()
        for tag_group in self.tag_groups:
            response.update(["%s/%s" % (tag_group.id, tag) for tag in tag_group.get_tag_ids()])

        response.update(self.aux_tag_list.get_tag_ids())
        return response

    def get_tag_or_aux_tag(self, tag_id):
        for tag_group in self.tag_groups:
            for grouped_tag in tag_group.tags:
                if grouped_tag.id == tag_id:
                    return grouped_tag

        for aux_tag in self.aux_tag_list.get_tags():
            if aux_tag.id == tag_id:
                return aux_tag

    def parse_config(self, data):
        self._initialize()
        if isinstance(data, dict):
            self._parse_from_dict(data)
        else:
            self._parse_legacy_format(data[0], data[1])

    def _parse_from_dict(self, tag_info):  # new style
        for tag_group in tag_info["tag_groups"]:
            self.tag_groups.append(TagGroup(tag_group))
        for aux_tag in tag_info["aux_tags"]:
            self.aux_tag_list.append(AuxTag(aux_tag))

    def _parse_legacy_format(self, taggroup_info, auxtags_info):  # legacy style
        for tag_group_tuple in taggroup_info:
            self.tag_groups.append(TagGroup(tag_group_tuple))

        for aux_tag_tuple in auxtags_info:
            self.aux_tag_list.append(AuxTag(aux_tag_tuple))

    # TODO: Change API to use __add__/__setitem__?
    def insert_tag_group(self, tag_group):
        # TODO: re-enable this!
        #if is_builtin_host_tag_group(tag_group.id):
        #    raise MKUserError("tag_id", _("You can not override a builtin tag group."))
        self._insert_tag_group(tag_group)

    def _insert_tag_group(self, tag_group):
        self.tag_groups.append(tag_group)
        self._validate_group(tag_group)

    def update_tag_group(self, tag_group):
        for idx, group in enumerate(self.tag_groups):
            if group.id == tag_group.id:
                self.tag_groups[idx] = tag_group
                break
        else:
            raise MKUserError("", _("Unknown tag group"))
        self._validate_group(tag_group)

    def validate_config(self):
        for tag_group in self.tag_groups:
            self._validate_group(tag_group)

        self.aux_tag_list.validate()
        self._validate_ids()

    def _validate_ids(self):
        """Make sure that no tag key is used twice as aux_tag ID or tag group id"""
        seen_ids = set()
        for tag_group in self.tag_groups:
            if tag_group.id in seen_ids:
                raise MKUserError("tag_id", _("The tag ID %s is used twice.") % tag_group.id)
            seen_ids.add(tag_group.id)

        for aux_tag in self.aux_tag_list.get_tags():
            if aux_tag.id in seen_ids:
                raise MKUserError("tag_id", _("The tag ID %s is used twice.") % aux_tag.id)
            seen_ids.add(aux_tag.id)

    # TODO: cleanup this mess
    # This validation is quite gui specific, I do not want to introduce this into the base classes
    def _validate_group(self, tag_group):
        if not tag_group.id:
            raise MKUserError("tag_id", _("Please specify an ID for your tag group."))
        _validate_tag_id(tag_group.id, "tag_id")

        if tag_group.id == "site":
            raise MKUserError("tag_id",
                              _("The tag group %s is reserved for internal use.") % tag_group.id)

        if not tag_group.title:
            raise MKUserError("title", _("Please specify a title for your tag group."))

        have_none_tag = False
        for nr, tag in enumerate(tag_group.tags):
            if tag.id or tag.title:
                if not tag.id:
                    tag.id = None

                    if len(tag_group.tags) == 1:
                        raise MKUserError("choices_%d_id" % (nr + 1),
                                          _("Can not use an empty tag ID with a single choice."))

                    if have_none_tag:
                        raise MKUserError("choices_%d_id" % (nr + 1),
                                          _("Only one tag may be empty."))

                    have_none_tag = True

                # Make sure tag ID is unique within this group
                for (n, x) in enumerate(tag_group.tags):
                    if n != nr and x.id == tag.id:
                        raise MKUserError(
                            "choices_id_%d" % (nr + 1),
                            _("Tags IDs must be unique. You've used <b>%s</b> twice.") % tag.id)

        if len(tag_group.tags) == 0:
            raise MKUserError("id_0", _("Please specify at least one tag."))
        if len(tag_group.tags) == 1 and tag_group.tags[0] is None:
            raise MKUserError("id_0", _("Tags with only one choice must have an ID."))

    def get_dict_format(self):
        result = {"tag_groups": [], "aux_tags": []}
        for tag_group in self.tag_groups:
            result["tag_groups"].append(tag_group.get_dict_format())

        result["aux_tags"] = self.aux_tag_list.get_dict_format()

        return result


class BuiltinAuxTagList(AuxTagList):
    def append(self, aux_tag):
        self._append(aux_tag)


class BuiltinTagConfig(TagConfig):
    def __init__(self):
        super(BuiltinTagConfig, self).__init__()
        self.parse_config({
            "tag_groups": self._builtin_tag_groups(),
            "aux_tags": self._builtin_aux_tags(),
        })

    def _initialize(self):
        self.tag_groups = []
        self.aux_tag_list = BuiltinAuxTagList()

    def _builtin_tag_groups(self):
        return [
            {
                'id': 'agent',
                'title': _('Check_MK Agent'),
                'topic': _('Data sources'),
                'tags': [
                    {
                        'id': 'cmk-agent',
                        'title': _('Normal Checkmk agent, or special agent if configured'),
                        'aux_tags': ['tcp'],
                    },
                    {
                        'id': 'all-agents',
                        'title': _('Normal Checkmk agent, all configured special agents'),
                        'aux_tags': ['tcp'],
                    },
                    {
                        'id': 'special-agents',
                        'title': _('No Checkmk agent, all configured special agents'),
                        'aux_tags': ['tcp'],
                    },
                    {
                        'id': 'no-agent',
                        'title': _('No agent'),
                        'aux_tags': [],
                    },
                ],
            },
            {
                'id': 'piggyback',
                'title': _("Piggyback"),
                'topic': _('Data sources'),
                'tags': [
                    {
                        "id": "auto-piggyback",
                        "title": _("Use piggyback data from other hosts if present"),
                        "aux_tags": []
                    },
                    {
                        "id": "piggyback",
                        "title": _("Always use and expect piggyback data"),
                        "aux_tags": [],
                    },
                    {
                        "id": "no-piggyback",
                        "title": _("Never use piggyback data"),
                        "aux_tags": [],
                    },
                ],
            },
            {
                'id': 'snmp_ds',
                'title': _('SNMP'),
                'topic': _('Data sources'),
                'tags': [{
                    'id': 'no-snmp',
                    'title': _('No SNMP'),
                    'aux_tags': [],
                }, {
                    'id': 'snmp-v2',
                    'title': _('SNMP v2 or v3'),
                    'aux_tags': ['snmp'],
                }, {
                    'id': 'snmp-v1',
                    'title': _('SNMP v1'),
                    'aux_tags': ['snmp'],
                }],
            },
            {
                'id': 'address_family',
                'title': _('IP Address Family'),
                'topic': u'Address',
                'tags': [
                    {
                        'id': 'ip-v4-only',
                        'title': _('IPv4 only'),
                        'aux_tags': ['ip-v4'],
                    },
                    {
                        'id': 'ip-v6-only',
                        'title': _('IPv6 only'),
                        'aux_tags': ['ip-v6'],
                    },
                    {
                        'id': 'ip-v4v6',
                        'title': _('IPv4/IPv6 dual-stack'),
                        'aux_tags': ['ip-v4', 'ip-v6'],
                    },
                    {
                        'id': 'no-ip',
                        'title': _('No IP'),
                        'aux_tags': [],
                    },
                ],
            },
        ]

    def _builtin_aux_tags(self):
        return [
            {
                'id': 'ip-v4',
                'topic': _('Address'),
                'title': _('IPv4'),
            },
            {
                'id': 'ip-v6',
                'topic': _('Address'),
                'title': _('IPv6'),
            },
            {
                'id': 'snmp',
                'topic': _('Data sources'),
                'title': _('Monitor via SNMP'),
            },
            {
                'id': 'tcp',
                'topic': _('Data sources'),
                'title': _('Monitor via Check_MK Agent'),
            },
            {
                'id': 'ping',
                'topic': _('Data sources'),
                'title': _('Only ping this device'),
            },
        ]

    def insert_tag_group(self, tag_group):
        self._insert_tag_group(tag_group)


def sample_tag_config():
    """Returns the WATO sample tag config

    The difference between the builtin and sample tag config is that the builtin
    tag configuration can not be changed by the user. The sample tag config is
    created on site creation (more precisely: during first WATO access).
    """
    return {
        'aux_tags': [],
        'tag_groups': [
            {
                'id': 'criticality',
                'tags': [{
                    'aux_tags': [],
                    'id': 'prod',
                    'title': u'Productive system'
                }, {
                    'aux_tags': [],
                    'id': 'critical',
                    'title': u'Business critical'
                }, {
                    'aux_tags': [],
                    'id': 'test',
                    'title': u'Test system'
                }, {
                    'aux_tags': [],
                    'id': 'offline',
                    'title': u'Do not monitor this host'
                }],
                'title': u'Criticality'
            },
            {
                'id': 'networking',
                'tags': [{
                    'aux_tags': [],
                    'id': 'lan',
                    'title': u'Local network (low latency)'
                }, {
                    'aux_tags': [],
                    'id': 'wan',
                    'title': u'WAN (high latency)'
                }, {
                    'aux_tags': [],
                    'id': 'dmz',
                    'title': u'DMZ (low latency, secure access)'
                }],
                'title': u'Networking Segment'
            },
        ],
    }
