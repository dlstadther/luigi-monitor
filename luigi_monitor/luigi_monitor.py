from __future__ import print_function
import inspect
import json
import logging
import os
import sys
import platform
from collections import defaultdict
from contextlib import contextmanager

import luigi
from luigi.retcodes import run_with_retcodes as run_luigi
import requests

logger = logging.getLogger('luigi-interface')

const_success_message = "Task ran successfully!"
const_failed_message = "Task failed!"
const_missing_message = "Task could not be completed!"


class luigi_monitor(luigi.Config):
    slack_url = luigi.Parameter(default=None)
    max_print = luigi.IntParameter(default=5)
    username = luigi.Parameter(default=None)


class Monitor:

    def __init__(self):
        self.recorded_events = defaultdict(list)
        self.notify_events = None
        self.core_module = None
        self.root_task = None
        self.root_task_parameters = None

    def is_success_only(self):
        success_only = True
        for k, i in self.recorded_events.items():
            if k == 'SUCCESS' and len(i) > 0:
                success_only = success_only and True
            elif len(i) > 0:
                success_only = success_only and False
                break
        return success_only

    def has_missing_tasks(self):
        return True if self.recorded_events['DEPENDENCY_MISSING'] else False

    def has_failed_tasks(self):
        return True if self.recorded_events['FAILURE'] else False


def discovered(task, dependency):
    raise NotImplementedError


def missing(task):
    task = str(task)
    m.recorded_events['DEPENDENCY_MISSING'].append(task)


def present(task):
    raise NotImplementedError


def broken(task, exception):
    raise NotImplementedError


def start(task):
    raise NotImplementedError


def failure(task, exception):
    task = str(task)
    failure = {'task': task, 'exception': str(exception)}
    m.recorded_events['FAILURE'].append(failure)


def success(task):
    task = str(task)
    m.recorded_events['FAILURE'] = [failure for failure in m.recorded_events['FAILURE']
                                    if task not in failure['task']]
    m.recorded_events['DEPENDENCY_MISSING'] = [missing for missing in m.recorded_events['DEPENDENCY_MISSING']
                                               if task not in missing]
    m.recorded_events['SUCCESS'].append(task)


def processing_time(task, time):
    raise NotImplementedError


event_map = {
    "DEPENDENCY_DISCOVERED": {"function": discovered, "handler": luigi.Event.DEPENDENCY_DISCOVERED},
    "DEPENDENCY_MISSING": {"function": missing, "handler": luigi.Event.DEPENDENCY_MISSING},
    "DEPENDENCY_PRESENT": {"function": present, "handler": luigi.Event.DEPENDENCY_PRESENT},
    "BROKEN_TASK": {"function": broken, "handler": luigi.Event.BROKEN_TASK},
    "START": {"function": start, "handler": luigi.Event.START},
    "FAILURE": {"function": failure, "handler": luigi.Event.FAILURE},
    "SUCCESS": {"function": success, "handler": luigi.Event.SUCCESS},
    "PROCESSING_TIME": {"function": processing_time, "handler": luigi.Event.PROCESSING_TIME}
}


def set_handlers(events):
    if not isinstance(events, list):
        raise Exception("events must be a list")

    for event in events:
        if event not in event_map:
            raise Exception("{} is not a valid event.".format(event))
        handler = event_map[event]['handler']
        function = event_map[event]['function']
        luigi.Task.event_handler(handler)(function)


# TODO: add configurability
def format_message(max_print, host):
    job = os.path.basename(inspect.stack()[-1][1])

    if host is None:
        host = platform.node()
    text = []
    emoji = ":x:"

    if m.has_failed_tasks() and 'FAILURE' in m.notify_events:
        text.append(add_context_to_message("failed", const_failed_message))
        text.append("\t\t\t*Failures:*")
        if len(m.recorded_events['FAILURE']) > int(max_print):
            text.append("\t\t\tMore than {} failures. Please check logs.".format(max_print))
        else:
            for failure in m.recorded_events['FAILURE']:
                text.append("\t\t\t\tTask: {}; Exception: {}".format(failure['task'], failure['exception']))

    if m.has_missing_tasks() and 'DEPENDENCY_MISSING' in m.notify_events:
        text.append(add_context_to_message("could not be completed", const_missing_message))
        text.append("\t\t\t*Tasks with missing dependencies:*")
        if len(m.recorded_events['DEPENDENCY_MISSING']) > int(max_print):
            text.append("\t\t\t\tMore than {} tasks with missing dependencies. Please check logs.".format(max_print))
        else:
            for missing in m.recorded_events['DEPENDENCY_MISSING']:
                text.append("\t\t\t\t" + missing)

    # if job successful add success message
    if m.is_success_only() and 'SUCCESS' in m.notify_events:
        emoji = ":heavy_check_mark:"
        text.append(add_context_to_message("ran successfully", const_success_message))
        text.append("\t\t\t*Following {} tasks succeeded:*".format(len(m.recorded_events['SUCCESS'])))
        for succeeded in m.recorded_events['SUCCESS']:
            text.append("\t\t\t\t{}".format(succeeded))

    fulltext = ["{} Status report for {} at *{}*:".format(emoji, job, host)]
    fulltext.extend(text)
    formatted_text = "\n".join(fulltext)
    if formatted_text == fulltext[0]:
        return False
    return formatted_text


def add_context_to_message(result, appendix):
    if m.root_task is None:
        return appendix

    message = ["\t\tTask *{}* ".format(m.root_task)]
    if m.core_module is not None:
        message.append("from module *{}* ".format(m.core_module))

    message.append(result)

    if m.root_task_parameters is not None:
        message.append(" with parameters:")
        for task_param in m.root_task_parameters:
            if task_param.startswith("--"):
                message.append("\n\t\t\t\t{}".format(task_param))
            else:
                message.append(" {}".format(task_param))
    else:
        message.append("!")
    return ''.join(message)


def send_message(**kwargs):
    slack_url = kwargs.get('slack_url')
    max_print = kwargs.get('max_print')
    username = kwargs.get('username')
    host = kwargs.get('host')

    msg = format_message(max_print, host)
    if not slack_url and msg:
        logger.warn("slack_url not provided. Message will not be sent.\n{}".format(msg))
        return False
    if msg:
        payload = {"text": msg}
        if username:
            payload['username'] = username
        r = requests.post(slack_url, data=json.dumps(payload))
        if r.status_code != 200:
            raise Exception(r.text)
    return True


m = Monitor()


@contextmanager
def monitor(events=['FAILURE', 'DEPENDENCY_MISSING', 'SUCCESS'], slack_url=None, max_print=5, username=None, host=None):
    if events:
        m.notify_events = events
        set_handlers(events)
    yield m
    kwargs = {'slack_url': slack_url, 'max_print': max_print, 'username': username, 'host': host}
    send_message(**kwargs)


def run():
    """Command line entry point for luigi-monitor"""
    events = ['FAILURE', 'DEPENDENCY_MISSING', 'SUCCESS']
    m.notify_events = events
    set_handlers(events)
    parse_sys_args(sys.argv)
    try:
        run_luigi(sys.argv[1:])
    except SystemExit:
        send_message(luigi_monitor.get_params())


def parse_sys_args(args):
    """Parse commandline arguments"""
    contains_core_module = False
    if len(args) >= 4:
        if args[1] == "--module":
            contains_core_module = True
            m.core_module = args[2]
    if contains_core_module:
        m.root_task = args[3]
        if len(args) > 4:
            m.root_task_parameters = args[4:]
    else:
        m.root_task = args[1]
        if len(args) > 2:
            m.root_task_parameters = args[2:]
