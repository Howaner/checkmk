#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset: 4 -*-
import os
import platform
import pytest
import re
import subprocess
from local import (actual_output, src_agent_exe, make_yaml_config, local_test, src_exec_dir,
                   wait_agent, write_config, root_dir, user_dir, get_main_yaml_name,
                   get_user_yaml_name)
import sys


class Globals(object):
    section = 'check_mk'
    alone = True
    output_file = 'agentoutput.txt'
    only_from = None
    ipv4_to_ipv6 = {'127.0.0.1': '0:0:0:0:0:ffff:7f00:1', '10.1.2.3': '0:0:0:0:0:ffff:a01:203'}


@pytest.fixture
def testfile():
    return os.path.basename(__file__)


@pytest.fixture(params=['alone', 'with_systemtime'])
def testconfig(request, make_yaml_config):
    Globals.alone = request.param == 'alone'
    if Globals.alone:
        make_yaml_config['global']['sections'] = Globals.section
    else:
        make_yaml_config['global']['sections'] = [Globals.section, "systemtime"]
    return make_yaml_config


@pytest.fixture
def testconfig_host(request, testconfig):
    return testconfig


@pytest.fixture(
    params=[None, '127.0.0.1 10.1.2.3'], ids=['only_from=None', 'only_from=127.0.0.1_10.1.2.3'])
def testconfig_only_from(request, testconfig_host):
    Globals.only_from = request.param
    if request.param:
        testconfig_host['global']['only_from'] = ['127.0.0.1', '10.1.2.3']
    else:
        testconfig_host['global']['only_from'] = None
    return testconfig_host


# live example of valid output
"""
<<<check_mk>>>
Version: 2.0.0i1
BuildDate: Jun  7 2019
AgentOS: windows
Hostname: SERG-DELL
Architecture: 32bit
WorkingDirectory: c:\\dev\\shared
ConfigFile: c:\\dev\\shared\\check_mk.yml
LocalConfigFile: C:\\ProgramData\\CheckMK\\Agent\\check_mk.user.yml
AgentDirectory: c:\\dev\\shared
PluginsDirectory: C:\\ProgramData\\CheckMK\\Agent\\plugins
StateDirectory: C:\\ProgramData\\CheckMK\\Agent\\state
ConfigDirectory: C:\\ProgramData\\CheckMK\\Agent\\config
TempDirectory: C:\\ProgramData\\CheckMK\\Agent\\temp
LogDirectory: C:\\Users\\Public
SpoolDirectory: C:\\ProgramData\\CheckMK\\Agent\\spool
LocalDirectory: C:\\ProgramData\\CheckMK\\Agent\\local
OnlyFrom: 0.0.0.0/0
"""


def make_only_from_array(ipv4):
    if ipv4 is None:
        return None

    addr_list = []

    # not very pythonic, but other methods(reduce) overkill
    for x in ipv4:
        addr_list.append(x)
        addr_list.append(Globals.ipv4_to_ipv6[x])

    return addr_list


@pytest.fixture
def expected_output():
    drive_letter = r'[A-Z]:'
    ipv4 = Globals.only_from.split() if Globals.only_from else None
    ___pip = make_only_from_array(ipv4)
    expected = [
        # Note: The first two lines are output with crash_debug = yes in 1.2.8
        # but no longer in 1.4.0:
        # r'<<<logwatch>>>\',
        # r'[[[Check_MK Agent]]]','
        r'<<<%s>>>' % Globals.section,
        r'Version: \d+\.\d+\.\d+([bi]\d+)?(p\d+)?',
        r'BuildDate: [A-Z][a-z]{2} (\d{2}| \d) \d{4}',
        r'AgentOS: windows',
        r'Hostname: .+',
        r'Architecture: \d{2}bit',
        r'WorkingDirectory: %s' % (re.escape(os.getcwd())),
        r'ConfigFile: %s' % (re.escape(get_main_yaml_name(root_dir))),
        r'LocalConfigFile: %s' % (re.escape(get_user_yaml_name(user_dir))),
        r'AgentDirectory: %s' % (re.escape(root_dir)),
        r'PluginsDirectory: %s' % (re.escape(os.path.join(user_dir, 'plugins'))),
        r'StateDirectory: %s' % (re.escape(os.path.join(user_dir, 'state'))),
        r'ConfigDirectory: %s' % (re.escape(os.path.join(user_dir, 'config'))),
        r'TempDirectory: %s' % (re.escape(os.path.join(user_dir, 'temp'))),
        r'LogDirectory: %s' % (re.escape('C:\\Users\\Public')),
        r'SpoolDirectory: %s' % (re.escape(os.path.join(user_dir, 'spool'))),
        r'LocalDirectory: %s' % (re.escape(os.path.join(user_dir, 'local'))),
        # r'ScriptStatistics: Plugin C:0 E:0 T:0 Local C:0 E:0 T:0',
        # Note: The following three lines are output with crash_debug = yes in
        # 1.2.8 but no longer in 1.4.0:
        # r'ConnectionLog: %s%s' %
        # (drive_letter,
        #  re.escape(os.path.join(exec_dir, 'log', 'connection.log'))),
        # r'CrashLog: %s%s' %
        # (drive_letter,
        #  re.escape(os.path.join(exec_dir, 'log', 'crash.log'))),
        # r'SuccessLog: %s%s' %
        # (drive_letter,
        #  re.escape(os.path.join(exec_dir, 'log', 'success.log'))),
        (r'OnlyFrom: %s/32 %s/128 %s/32 %s/128' % tuple([i4 for i4 in make_only_from_array(ipv4)])
         if Globals.only_from else r'OnlyFrom: 0\.0\.0\.0/0')
    ]
    if not Globals.alone:
        expected += [re.escape(r'<<<systemtime>>>'), r'\d+']
    return expected


def test_section_check_mk(request, testconfig_only_from, expected_output, actual_output, testfile):
    # request.node.name gives test name
    print(actual_output)
    local_test(expected_output, actual_output, testfile, request.node.name)
