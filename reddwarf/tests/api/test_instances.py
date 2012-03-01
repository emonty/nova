#    Copyright 2011 OpenStack LLC
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""
Tests for Instances API calls
"""

import mox
import json
import stubout
import webob
from paste import urlmap

import nova
from nova import context
from nova import test
from nova.compute import vm_states
from nova.compute import power_state
import nova.exception as nova_exception


import reddwarf
import reddwarf.exception as exception
from reddwarf.api import instances
from reddwarf.db import models
from reddwarf.tests import util

#instances_url = util.v1_instances_prefix
instances_url = r"/v1.0/dbaas/instances"

def localid_from_uuid(id):
    return id

def compute_delete(self, ctxt, id):
    return

def compute_get(self, ctxt, id):
    return {'vm_state': vm_states.ACTIVE,
            'id': 1,}

def compute_get_exception(self, ctxt, id):
    raise nova_exception.NotFound()

def compute_get_building(self, ctxt, id):
    return {'vm_state': vm_states.BUILDING,
            'id': 1,
            }
    
def compute_get_osclient_not_found(osclient, id):
    raise exception.NotFound()

def compute_get_osclient_unprocessable(osclient, id):
    raise exception.UnprocessableEntity()

def compute_get_osclient_accepted(osclient, id):
#    return webob.exc.HTTPAccepted()
    return

def guest_status_get_running(id, session=None):
    status = models.GuestStatus()
    status.state = power_state.RUNNING
    return status

def guest_status_get_failed(id, session=None):
    status = models.GuestStatus()
    status.state = power_state.FAILED
    return status

def guest_get_mapping_deleting(a, b):
    return {"1": "ACTIVE (deleting)"}

def request_obj(url, method, body={}):
    req = webob.Request.blank(url)
    req.method = method
    if method in ['POST', 'PUT']:
        req.body = json.dumps(body)
    req.headers["content-type"] = "application/json"
    return req

def get_osclient_show(osclient, id):
    response = DummyServer()
    return response

class DummyServer(object):
    
    def __init__(self):
        self.id = 11111
        self.status = "ACTIVE (deleting)"

class InstanceApiTest(test.TestCase):
    """Test various Database API calls"""

    def setUp(self):
        super(InstanceApiTest, self).setUp()
        self.context = context.get_admin_context()
        self.controller = instances.Controller()
        self.stubs.Set(reddwarf.db.api, "localid_from_uuid", localid_from_uuid)
        self.stubs.Set(nova.compute.API, "get", compute_get)

    def tearDown(self):
        self.stubs.UnsetAll()
        super(InstanceApiTest, self).tearDown()

    def test_instances_delete_not_found(self):
        self.stubs.Set(nova.compute.API, "get", compute_get_exception)
        self.stubs.Set(reddwarf.client.osclient.OSClient, "delete", compute_get_osclient_not_found)
        self.stubs.Set(reddwarf.client.osclient.OSClient, "show", get_osclient_show)
        self.stubs.Set(reddwarf.api.instances.Controller, 
                       "get_guest_state_mapping", 
                       guest_get_mapping_deleting)
        req = request_obj('%s/1' % instances_url, 'DELETE')
        res = req.get_response(util.wsgi_app(fake_auth_context=self.context))
        self.assertEqual(res.status_int, 404)

    def test_instances_delete_unprocessable(self):
        self.stubs.Set(nova.compute.API, "get", compute_get_building)
        self.stubs.Set(reddwarf.client.osclient.OSClient, "delete", compute_get_osclient_unprocessable)
        self.stubs.Set(reddwarf.client.osclient.OSClient, "show", get_osclient_show)
        self.stubs.Set(reddwarf.api.instances.Controller, 
                       "get_guest_state_mapping", 
                       guest_get_mapping_deleting)
        #self.stubs.Set(reddwarf.db.api, "guest_status_get", guest_status_get_running)
        req = request_obj('%s/1' % instances_url, 'DELETE')
        res = req.get_response(util.wsgi_app(fake_auth_context=self.context))
        self.assertEqual(res.status_int, 422)

    def test_instances_delete_failed(self):
        self.stubs.Set(nova.compute.API, "delete", compute_delete)
        self.stubs.Set(nova.compute.API, "get", compute_get_building)
        self.stubs.Set(reddwarf.client.osclient.OSClient, "delete", compute_get_osclient_accepted)
        self.stubs.Set(reddwarf.client.osclient.OSClient, "show", get_osclient_show)
        self.stubs.Set(reddwarf.api.instances.Controller, 
                       "get_guest_state_mapping", 
                       guest_get_mapping_deleting)
        req = request_obj('%s/1' % instances_url, 'DELETE')
        res = req.get_response(util.wsgi_app(fake_auth_context=self.context))
        self.assertEqual(res.status_int, 202)