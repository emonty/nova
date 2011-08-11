# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2011 Openstack, LLC.
# All Rights Reserved.
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
Dns Driver that uses Rackspace DNSaaS.
"""


#from nova.dns.driver import DnsEntry
#from nova.dns.driver import DnsZone
from novaclient.exceptions import NotFound
from rsdns.client import DNSaas

from nova import flags
from nova import utils
from nova.dns.driver import DnsEntry
flags.DEFINE_string('dns_hostname', 'dbaas-test-domain.com',
                     'Hostname base for hosts thru DNSaas')
flags.DEFINE_string('dns_account_id', 0,
                     'System account id for domain modification thru DNSaaS.')
flags.DEFINE_string('dns_auth_url', 'https://auth.api.rackspacecloud.com/v1.0',
                     'System account id for domain modification thru DNSaaS.')
flags.DEFINE_string('dns_domain_name', "blah",
                    'Domain name for the root domain through DNSaaS.')
flags.DEFINE_string('dns_username', '',
                     'System account user for domain modification thru DNSaas')
flags.DEFINE_string('dns_passkey', '',
                     'System account user for domain modification thru DNSaas')
flags.DEFINE_string('dns_management_base_url', None,
                    'The management URL for DNS.')
FLAGS = flags.FLAGS


class EntryToRecordConverter(object):

    def __init__(self, default_dns_zone):
        self.default_dns_zone = default_dns_zone

    def domain_to_dns_zone(self, domain):
       return RsDnsZone(id=domain.id, name=domain.name)

    def name_to_long_name(self, name, dns_zone=None):
        dns_zone = dns_zone or self.default_dns_zone
        if name:
            long_name = name + "." + dns_zone.name
        else:
            long_name = ""
        return long_name

    def record_to_entry(self, record, dns_zone):
        # end_index = len(record.name) - (len(dns_zone.name) + 1)
        # if end_index >= 0:
        #    entry_name = record.name[0:end_index]
        #else:
        #    entry_name = None
        entry_name = record.name
        return DnsEntry(name=entry_name, content=record.data, type=record.type,
                        ttl=record.ttl, dns_zone=dns_zone)


def create_client_with_flag_values():
    """Creates a RS DNSaaS client using the Flag values."""
    if FLAGS.dns_management_base_url == None:
        raise RuntimeError("Missing flag value for dns_management_base_url.")
    return DNSaas(FLAGS.dns_account_id, FLAGS.dns_username, FLAGS.dns_passkey,
                  auth_url=FLAGS.dns_auth_url,
                  management_base_url = FLAGS.dns_management_base_url)


def find_default_zone(dns_client, raise_if_zone_missing=True):
    """Using the domain_name from the FLAG values, creates a zone.

    Because RS DNSaaS needs the ID, we need to find this value before we start.
    In testing it's difficult to keep up with it because the database keeps
    getting wiped... maybe later we could go back to storing it as a FLAG value.

    """
    domain_name = FLAGS.dns_domain_name
    try:
        domains = dns_client.domains.list() # name=domain_name)
        for domain in domains:
            if domain.name == domain_name:
                return RsDnsZone(id = domain.id, name = domain_name)
    except NotFound:
        pass
    if not raise_if_zone_missing:
        return RsDnsZone(id = None, name = domain_name)
    raise RuntimeError("The dns_domain_name from the FLAG values (%s) "
                       "does not exist!  account_id=%s, username=%s, LIST=%s"
        % (domain_name, FLAGS.dns_account_id, FLAGS.dns_username, str(domains)) )

class RsDnsDriver(object):
    """Uses RS DNSaaS"""

    def __init__(self, raise_if_zone_missing=True):
        self.dns_client = create_client_with_flag_values()
        self.dns_client.authenticate()
        self.default_dns_zone = find_default_zone(self.dns_client,
                                                  raise_if_zone_missing)
        self.converter = EntryToRecordConverter(self.default_dns_zone)

    def create_entry(self, entry):
        dns_zone = entry.dns_zone or self.default_dns_zone
        if dns_zone.id == None:
            raise TypeError("The entry's dns_zone must have an ID specified.")
        name = entry.name # + "." + dns_zone.name
        self.dns_client.records.create(domain=dns_zone.id,
                                       record_name=name,
                                       record_data=entry.content,
                                       record_type=entry.type,
                                       record_ttl=entry.ttl)

    def delete_entry(self, name, type, dns_zone=None):
        dns_zone = dns_zone or self.default_dns_zone
        long_name = name
        records = self.dns_client.records.list(domain_id=dns_zone.id,
                                               record_name=long_name,
                                               record_type=type)
        for record in records:
            self.dns_client.records.delete(domain_id=dns_zone.id,
                                           record_id=record.id)

    def get_entries(self, name=None, content=None, dns_zone=None):
        dns_zone = dns_zone or self.default_dns_zone
        long_name = name # self.converter.name_to_long_name(name)
        records = self.dns_client.records.list(domain_id=dns_zone.id,
                                               record_name=long_name,
                                               record_address=content)
        return [self.converter.record_to_entry(record, dns_zone)
                for record in records]

    def get_entries_by_content(self, content, dns_zone=None):
        return self.get_entries(content=content)

    def get_entries_by_name(self, name, dns_zone=None):
        return self.get_entries(name=name, dns_zone=dns_zone)

    def get_dns_zones(self, name=None):        
        domains = self.dns_client.domains.list(name=name)
        return [self.converter.domain_to_dns_zone(domain) for domain in domains]

    def modify_content(self, *args, **kwargs):
        raise NotImplementedError("Not implemented for RS DNS.")

    def rename_entry(self, *args, **kwargs):
        raise NotImplementedError("Not implemented for RS DNS.")


class RsDnsInstanceEntryFactory(object):
    """Defines how instance DNS entries are created for instances.

    {user_id}-{display_name}.{NAMED_BASE_URL}
    NAMED_BASE_URL, As per Bartels, is created like so:

    {DC}.{PRODUCT}.rackspace.net

    example:
    ord1.lbaas.rackspace.net
    ord1.dnsaas.rackspace.net

    """

    def __init__(self):
        dns_client = create_client_with_flag_values()
        dns_client.authenticate()
        self.default_dns_zone = find_default_zone(dns_client)

    def create_entry(self, instance):
        # Don't need the driver param here.

        # TODO (mbasnight): This should be extracted to a utility method so the
        #                   ovz conn can use it.
        # TODO (mbasnight): This, as-is wont work unless we are very strict with
        #                   what constitutes a valid display_name
        #hostname = "%s-%s.%s" % (instance['user_id'], instance['display_name'],
        #                         FLAGS.dns_hostname)
        user_id = instance.get('user_id', None)
        id = instance.get('id', None)
        if not user_id:
            raise ValueError('"user_id" not found or empty in instance.')
        if not id:
            raise ValueError('"id" not found or empty in instance.')
        hostname = ("%s-%s" % (user_id, id)) + "." + self.default_dns_zone.name
        # TODO (mbasnight): Figure out what to do with the initial TTLs
        # TODO (mbasnight): Remove this hack by fixing the code above it so the
        #                   ip can be gotten without a sleep, ugh
        import time
        time.sleep(1)
        #ip = self.db_api.instance_get_fixed_address(ctxt, int(server_id))
        # No longer necessary- the manager will add the ip as the content.
        #LOG.info("returned ip %s" % ip)
        return DnsEntry(name=hostname, content=None, type="A",
                        priority=3600, dns_zone=self.default_dns_zone)

        # FLAGS.dns_domain_id)
        ##result = self.dns_client.records.create(FLAGS.dns_domain_id, hostname,
        ##                                        ip, "A", 3600)
        ### TODO (mbasnight): Do a verification check on the result == OK
        #                    (for now)
        ### TODO (mbasnight): This will eventually be a nicer object returned
        #                     that we can validate
        ##LOG.info("finished result for dns create %s" %result)
        ### TODO (mbasnight): Put the hostname in the return object

        ##return DnsEntry(name=instance.name, content=None, type="CNAME")


class RsDnsZone(object):

    def __init__(self, id, name):
        self.name = name
        self.id = id

    def __eq__(self, other):
        return isinstance(other, RsDnsZone) and \
               self.name == other.name and self.id == other.id

    def __str__(self):
        return "%s:%s" % (self.id, self.name)