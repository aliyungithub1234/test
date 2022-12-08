#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
    ESD
    ~~~

    Implements enumeration sub domains

    :author:    Feei <feei@feei.cn>
    :homepage:  https://github.com/FeeiCN/ESD
    :license:   GPL, see LICENSE for more details.
    :copyright: Copyright (c) 2018 Feei. All rights reserved
"""
import os
import re
import sys
import time
import ssl
import math
import string
import random
import traceback
import itertools
import datetime
import colorlog
import asyncio
import aiodns
import aiohttp
import logging
import requests
import backoff
import socket
import async_timeout
import dns.query
import dns.zone
import dns.resolver
from tqdm import tqdm
from colorama import Fore
from optparse import OptionParser
from aiohttp.resolver import AsyncResolver
from itertools import islice
from difflib import SequenceMatcher
import threading

__version__ = '0.0.29'

handler = colorlog.StreamHandler()
formatter = colorlog.ColoredFormatter(
    '%(log_color)s%(asctime)s [%(name)s] [%(levelname)s] %(message)s%(reset)s',
    datefmt=None,
    reset=True,
    log_colors={
        'DEBUG': 'cyan',
        'INFO': 'green',
        'WARNING': 'yellow',
        'ERROR': 'red',
        'CRITICAL': 'red,bg_white',
    },
    secondary_log_colors={},
    style='%'
)
handler.setFormatter(formatter)

logger = colorlog.getLogger('ESD')
logger.addHandler(handler)
logger.setLevel(logging.INFO)

ssl.match_hostname = lambda cert, hostname: True


# 只采用了递归，速度非常慢，在优化完成前不建议开启
# TODO:优化DNS查询，递归太慢了
class DNSQuery(object):
    def __init__(self, root_domain, subs, suffix):
        # root domain
        self.suffix = suffix
        self.sub_domains = []
        if root_domain:
            self.sub_domains.append(root_domain)

        for sub in subs:
            sub = ''.join(sub.rsplit(suffix, 1)).rstrip('.')
            self.sub_domains.append(f'{sub}.{suffix}')

    def dns_query(self):
        final_list = []
        soa = aaaa = txt = mx = []
        for subdomain in self.sub_domains:
            try:
                soa = []
                q_soa = dns.resolver.resolve(subdomain, 'SOA')
                for a in q_soa:
                    soa.append(str(a.rname).strip('.'))
                    soa.append(str(a.mname).strip('.'))
            except Exception as e:
                logger.warning(f'Query failed. {str(e)}')
            try:
                aaaa = []
                q_aaaa = dns.resolver.resolve(subdomain, 'AAAA')
                aaaa = [str(a.address).strip('.') for a in q_aaaa]
            except Exception as e:
                logger.warning(f'Query failed. {str(e)}')
            try:
                txt = []
                q_txt = dns.resolver.resolve(subdomain, 'TXT')
                txt = [t.strings[0].decode('utf-8').strip('.') for t in q_txt]
            except Exception as e:
                logger.warning(f'Query failed. {str(e)}')
            try:
                mx = []
                q_mx = dns.resolver.resolve(subdomain, 'MX')
                mx = [str(m.exchange).strip('.') for m in q_mx]
            except Exception as e:
                logger.warning('Query failed. {e}'.format(e=str(e)))
            domain_set = soa + aaaa + txt + mx
            domain_list = [i for i in domain_set]
            for p in domain_set:
                re_domain = re.findall(r'^(([a-z0-9]+(-[a-z0-9]+)*\.)+[a-z]{2,}\.?)$', p)
                if len(re_domain) > 0 and subdomain in re_domain[0][0]:
                    continue
                else:
                    domain_list.remove(p)
            final_list = domain_list + final_list
        # 递归调用，在子域名的dns记录中查找新的子域名
        recursive = []
        # print("before: {0}".format(final_list))
        # print("self.sub_domain: {0}".format(self.sub_domains))
        final_list = list(set(final_list).difference(set(self.sub_domains)))
        # print("after: {0}".format(final_list))
        if final_list:
            d = DNSQuery('', final_list, self.suffix)
            recursive = d.dns_query()
        return final_list + recursive


class DNSTransfer(object):
    def __init__(self, domain):
        self.domain = domain

    def transfer_info(self):
        ret_zones = list()
        try:
            nss = dns.resolver.resolve(self.domain, 'NS')
            nameservers = [str(ns) for ns in nss]
            ns_addr = dns.resolver.resolve(nameservers[0], 'A')
            # dnspython 的 bug，需要设置 lifetime 参数
            zones = dns.zone.from_xfr(dns.query.xfr(ns_addr, self.domain, relativize=False, timeout=2, lifetime=2),
                                      check_origin=False)
            names = zones.nodes.keys()
            for n in names:
                subdomain = ''
                for t in range(0, len(n) - 1):
                    if subdomain != '':
                        subdomain += '.'
                    subdomain += str(n[t].decode())
                if subdomain != self.domain:
                    ret_zones.append(subdomain)
            return ret_zones
        except BaseException:
            return []


class CAInfo(object):
    def __init__(self, domain):
        self.domain = domain

    def dns_resolve(self):
        padding_domain = 'www.' + self.domain
        # loop = asyncio.get_event_loop()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        resolver = aiodns.DNSResolver(loop=loop)
        f = resolver.query(padding_domain, 'A')
        result = loop.run_until_complete(f)
        return result[0].host

    def get_cert_info_by_ip(self, ip):
        s = socket.socket()
        s.settimeout(2)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        cert_path = base_dir + '/cacert.pem'
        connect = ssl.wrap_socket(s, cert_reqs=ssl.CERT_REQUIRED, ca_certs=cert_path)
        connect.settimeout(2)
        connect.connect((ip, 443))
        cert_data = connect.getpeercert().get('subjectAltName')
        return cert_data

    def get_ca_domain_info(self):
        domain_list = list()
        try:
            ip = self.dns_resolve()
            cert_data = self.get_cert_info_by_ip(ip)
        except Exception as e:
            return domain_list

        for domain_info in cert_data:
            hostname = domain_info[1]
            if not hostname.startswith('*') and hostname.endswith(self.domain):
                domain_list.append(hostname)

        return domain_list

    def get_subdomains(self):
        subs = list()
        subdomain_list = self.get_ca_domain_info()
        for sub in subdomain_list:
            subs.append(sub[:len(sub) - len(self.domain) - 1])
        return subs

domain_domain_ips = []
task_flag = False
class EnumSubDomain(threading.Thread):
    def __init__(self, domain, response_filter=None, dns_servers=None, skip_rsc=False, debug=False,
                 split=None, proxy=None, multiresolve=False):
        threading.Thread.__init__(self)
        self.project_directory = os.path.abspath(os.path.dirname(__file__))
        self.proxy = proxy
        self.data = {}
        self.domain = domain
        self.skip_rsc = skip_rsc
        self.split = split
        self.multiresolve = multiresolve
        self.stable_dns_servers = ['119.29.29.29']
        if dns_servers is None:
            # 除了DNSPod外，其它的都不适合作为稳定的DNS
            # 要么并发会显著下降，要么就完全不能用
            dns_servers = [
                '119.29.29.29',  # DNSPod https://www.dnspod.cn/products/public.dns
            ]

        random.shuffle(dns_servers)
        self.dns_servers = dns_servers
        self.resolver = None
        self.loop = asyncio.get_event_loop()
        self.general_dicts = []
        # Mark whether the current domain name is a pan-resolved domain name
        self.is_wildcard_domain = False
        # Use a nonexistent domain name to determine whether
        # there is a pan-resolve based on the DNS resolution result
        self.wildcard_sub = 'feei-esd-{random}'.format(random=random.randint(0, 9999))
        self.wildcard_sub3 = 'feei-esd-{random}.{random}'.format(random=random.randint(0, 9999))
        # There is no domain name DNS resolution IP
        self.wildcard_ips = []
        # No domain name response HTML
        self.wildcard_html = None
        self.wildcard_html_len = 0
        self.wildcard_html3 = None
        self.wildcard_html3_len = 0
        # Subdomains that are consistent with IPs that do not have domain names
        self.wildcard_subs = []
        # Wildcard domains use RSC
        self.wildcard_domains = {}
        # Corotines count
        self.coroutine_count = None
        # 并发太高DNS Server的错误会大幅增加
        self.coroutine_count_dns = 1000
        self.coroutine_count_request = 100
        # dnsaio resolve timeout
        self.resolve_timeout = 3
        # RSC ratio
        self.rsc_ratio = 0.8
        self.remainder = 0
        self.count = 0
        # Request Header
        self.request_headers = {
            'Connection': 'keep-alive',
            'Pragma': 'no-cache',
            'Cache-Control': 'no-cache',
            'Upgrade-Insecure-Requests': '1',
            'User-Agent': 'Baiduspider',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
            'DNT': '1',
            'Referer': 'http://www.baidu.com/',
            'Accept-Encoding': 'gzip, deflate',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'}
        # Filter the domain's response(regex)
        self.response_filter = response_filter
        # debug mode
        self.debug = debug
        if self.debug:
            logger.setLevel(logging.DEBUG)
        # collect redirecting domains and response domains
        self.domains_rs = []
        self.domains_rs_processed = []
        self.dns_query_errors = 0

    def generate_general_dicts(self, line):
        """
        Generate general subdomains dicts
        :param line:
        :return:
        """
        # 根据RFC 1034/1035规定，域名中仅允许出现字母、数字和横杠（-）
        letter_count = line.count('{letter}')
        number_count = line.count('{number}')
        letters = itertools.product(string.ascii_lowercase + '-', repeat=letter_count)
        letters = [''.join(l) for l in letters]
        numbers = itertools.product(string.digits, repeat=number_count)
        numbers = [''.join(n) for n in numbers]
        for l in letters:
            iter_line = line.replace('{letter}' * letter_count, l)
            # 根据RFC 1034/1035规定，子域名的头部和尾部不允许出现横杠（-），中间不允许连续出现横杠（-）
            iter_line = iter_line.strip('-')
            iter_line = re.sub(r'-+', '-', iter_line)
            if iter_line != '':
                self.general_dicts.append(iter_line)
        number_dicts = []
        for gd in self.general_dicts:
            for n in numbers:
                iter_line = gd.replace('{number}' * number_count, n)
                number_dicts.append(iter_line)
        if len(number_dicts) > 0:
            return number_dicts
        else:
            return self.general_dicts

    def load_sub_domain_dict(self):
        """
        Load subdomains from files and dicts
        :return:
        """
        dicts = []
        if self.debug:
            path = '{pd}/subs-test.esd'.format(pd=self.project_directory)
        else:
            path = '{pd}/subs.esd'.format(pd=self.project_directory)
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip().lower()
                # skip comments and space
                if '#' in line or line == '':
                    continue
                if '{letter}' in line or '{number}' in line:
                    self.general_dicts = []
                    dicts_general = self.generate_general_dicts(line)
                    dicts += dicts_general
                else:
                    # compatibility other dicts
                    line = line.strip('.')
                    dicts.append(line)
        dicts = list(set(dicts))

        # split dict
        if self.split is not None:
            s = self.split.split('/')
            dicts_choose = int(s[0])
            dicts_count = int(s[1])
            dicts_every = int(math.ceil(len(dicts) / dicts_count))
            dicts = [dicts[i:i + dicts_every] for i in range(0, len(dicts), dicts_every)][dicts_choose - 1]
            logger.info(f'Sub domain dict split {dicts_count} and get {dicts_choose}st')

        # root domain
        dicts.append('@')

        return dicts

    async def query(self, sub):
        """
        Query domain
        :param sub:
        :return:
        """
        global domain_domain_ips
        ret = None
        # root domain
        if sub == '@' or sub == '':
            sub_domain = self.domain
        else:
            sub = ''.join(sub.rsplit(self.domain, 1)).rstrip('.')
            sub_domain = f'{sub}.{self.domain}'
        # 如果存在特定异常则进行重试
        for i in range(4):
            try:
                ret = await self.resolver.query(sub_domain, 'A')
            except aiodns.error.DNSError as e:
                err_code, err_msg = e.args[0], e.args[1]
                # 域名确实不存在
                # 4:  Domain name not found
                # 1:  DNS server returned answer with no data
                # 其它情况都需要重试，否则存在很高的遗漏
                # 11: Could not contact DNS servers
                # 12: Timeout while contacting DNS servers
                if err_code not in [1, 4]:
                    if i == 2:
                        logger.warning(f'Try {i + 1} times, but failed. {sub_domain} {e}')
                        self.dns_query_errors = self.dns_query_errors + 1
                    continue
            except Exception as e:
                logger.info(sub_domain)
                logger.warning(traceback.format_exc())
            else:
                ret = [r.host for r in ret]
                domain_ips = [s for s in ret]
                # It is a wildcard domain name and
                # the subdomain IP that is burst is consistent with the IP
                # that does not exist in the domain name resolution,
                # the response similarity is discarded for further processing.
                if self.is_wildcard_domain and (
                        sorted(self.wildcard_ips) == sorted(domain_ips) or set(domain_ips).issubset(
                    self.wildcard_ips)):
                    if self.skip_rsc:
                        logger.debug(
                            f'{sub_domain} maybe wildcard subdomain, but it is --skip-rsc mode now, it will be drop this subdomain in results')
                    else:
                        logger.debug(f'{self.remainder} maybe wildcard domain, continue RSC {sub_domain}')
                else:
                    if sub != self.wildcard_sub:
                        self.data[sub_domain] = sorted(domain_ips)
                        print('', end='\n')
                        self.count += 1
                        logger.info(f'{self.remainder} {len(self.data)} {sub_domain} {domain_ips}')
                        domain_domain_ips.append((sub_domain,domain_ips))
            break
        self.remainder += -1
        return sub_domain, ret

    @staticmethod
    def limited_concurrency_coroutines(coros, limit):
        futures = [
            asyncio.ensure_future(c)
            for c in islice(coros, 0, limit)
        ]

        async def first_to_finish():
            while True:
                await asyncio.sleep(0)
                for f in futures:
                    if f.done():
                        futures.remove(f)
                        try:
                            nf = next(coros)
                            futures.append(asyncio.ensure_future(nf))
                        except StopIteration:
                            pass
                        return f.result()

        while len(futures) > 0:
            yield first_to_finish()

    async def start_e(self, tasks, tasks_num):
        """
        Limit the number of coroutines for reduce memory footprint
        :param tasks:
        :return:
        """
        for res in tqdm(self.limited_concurrency_coroutines(tasks, self.coroutine_count),
                        bar_format="%s{l_bar}%s{bar}%s{r_bar}%s" % (Fore.YELLOW, Fore.YELLOW, Fore.YELLOW, Fore.RESET),
                        total=tasks_num):
            await res

    @staticmethod
    def data_clean(data):
        try:
            html = re.sub(r'\s', '', data)
            html = re.sub(r'<script(?!.*?src=).*?>.*?</script>', '', html)
            return html
        except BaseException:
            return data

    @staticmethod
    @backoff.on_exception(backoff.expo, TimeoutError, max_tries=3)
    async def fetch(session, url):
        """
        Fetch url response with session
        :param session:
        :param url:
        :return:
        """
        try:
            async with async_timeout.timeout(20):
                async with session.get(url) as response:
                    return await response.text(), response.history
        except Exception as e:
            # TODO 当在随机DNS场景中只做响应相似度比对的话，如果域名没有Web服务会导致相似度比对失败从而丢弃
            logger.warning(f'fetch exception: {type(e).__name__} {url}')
            return None, None

    async def similarity(self, sub):
        """
        Enumerate subdomains by responding to similarities
        :param sub:
        :return:
        """
        # root domain
        if sub == '@' or sub == '':
            sub_domain = self.domain
        else:
            sub = ''.join(sub.rsplit(self.domain, 1)).rstrip('.')
            sub_domain = f'{sub}.{self.domain}'

        if sub_domain in self.domains_rs:
            self.domains_rs.remove(sub_domain)
        full_domain = f'http://{sub_domain}'
        # 如果跳转中的域名是以下情况则不加入下一轮RSC
        skip_domain_with_history = [
            # 跳到主域名了
            '{domain}'.format(domain=self.domain),
            'www.{domain}'.format(domain=self.domain),
            # 跳到自己本身了，比如HTTP跳HTTPS
            '{domain}'.format(domain=sub_domain),
        ]
        try:
            regex_domain = r"((?!\/)(?:(?:[a-z\d-]*\.)+{d}))".format(d=self.domain)
            resolver = AsyncResolver(nameservers=self.dns_servers)
            conn = aiohttp.TCPConnector(resolver=resolver)
            async with aiohttp.ClientSession(connector=conn, headers=self.request_headers) as session:
                html, history = await self.fetch(session, full_domain)
                html = self.data_clean(html)
                if history is not None and len(history) > 0:
                    location = str(history[-1].headers['location'])
                    if '.' in location:
                        location_split = location.split('/')
                        if len(location_split) > 2:
                            location = location_split[2]
                        else:
                            location = location
                        try:
                            location = re.match(regex_domain, location).group(0)
                        except AttributeError:
                            location = location
                        status = history[-1].status
                        if location in skip_domain_with_history and len(history) >= 2:
                            logger.debug(f'domain in skip: {sub_domain} {status} {location}')
                            return
                        else:
                            # cnsuning.com suning.com
                            if location[-len(self.domain) - 1:] == '.{d}'.format(d=self.domain):
                                # collect redirecting's domains
                                if sub_domain != location and location not in self.domains_rs and location not in self.domains_rs_processed:
                                    print('', end='\n')
                                    logger.info(
                                        f'[{sub_domain}] add redirect domain: {location}({len(self.domains_rs)})')
                                    self.domains_rs.append(location)
                                    self.domains_rs_processed.append(location)
                            else:
                                print('', end='\n')
                                logger.info(f'not same domain: {location}')
                    else:
                        print('', end='\n')
                        logger.info(f'not domain(maybe path): {location}')
                if html is None:
                    print('', end='\n')
                    logger.warning(f'domain\'s html is none: {sub_domain}')
                    return
                # collect response html's domains
                response_domains = re.findall(regex_domain, html)
                response_domains = list(set(response_domains) - {sub_domain})
                for rd in response_domains:
                    rd = rd.strip().strip('.')
                    if rd.count('.') >= sub_domain.count('.') and rd[-len(sub_domain):] == sub_domain:
                        continue
                    if rd not in self.domains_rs:
                        if rd not in self.domains_rs_processed:
                            print('', end='\n')
                            logger.info(f'[{sub_domain}] add response domain: {rd}({len(self.domains_rs)})')
                            self.domains_rs.append(rd)
                            self.domains_rs_processed.append(rd)

                if len(html) == self.wildcard_html_len:
                    ratio = 1
                else:
                    # SPEED 4 2 1, but here is still the bottleneck
                    # real_quick_ratio() > quick_ratio() > ratio()
                    # TODO bottleneck
                    if sub.count('.') == 0:  # secondary sub, ex: www
                        ratio = SequenceMatcher(None, html, self.wildcard_html).real_quick_ratio()
                        ratio = round(ratio, 3)
                    else:  # tertiary sub, ex: home.dev
                        ratio = SequenceMatcher(None, html, self.wildcard_html3).real_quick_ratio()
                        ratio = round(ratio, 3)
                self.remainder += -1
                if ratio > self.rsc_ratio:
                    # passed
                    logger.debug(f'{self.remainder} RSC ratio: {ratio} (passed) {sub_domain}')
                else:
                    # added
                    # for def distinct func
                    # self.wildcard_domains[sub_domain] = html
                    if self.response_filter is not None:
                        for resp_filter in self.response_filter.split(','):
                            if resp_filter in html:
                                logger.debug(f'{self.remainder} RSC filter in response (passed) {sub_domain}')
                                return
                            else:
                                continue
                        self.data[sub_domain] = self.wildcard_ips
                    else:
                        self.data[sub_domain] = self.wildcard_ips
                    print('', end='\n')
                    logger.info(f'{self.remainder} RSC ratio: {ratio} (added) {sub_domain}')
        except Exception as e:
            logger.debug(traceback.format_exc())
            return

    def distinct(self):
        for domain, html in self.wildcard_domains.items():
            for domain2, html2 in self.wildcard_domains.items():
                ratio = SequenceMatcher(None, html, html2).real_quick_ratio()
                if ratio > self.rsc_ratio:
                    # remove this domain
                    if domain2 in self.data:
                        del self.data[domain2]
                    m = 'Remove'
                else:
                    m = 'Stay'
                logger.info(f'{domain} : {domain2} {ratio} {m}')

    def check(self, dns):
        logger.info(f"Checking if DNS server {dns} is available")
        msg = b'\x5c\x6d\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x03www\x05baidu\x03com\x00\x00\x01\x00\x01'
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3)
        repeat = {
            1: 'first',
            2: 'second',
            3: 'third'
        }
        for i in range(3):
            logger.info(f"Sending message to DNS server a {repeat[i + 1]} time")
            sock.sendto(msg, (dns, 53))
            try:
                sock.recv(4096)
                break
            except socket.timeout as e:
                logger.warning('check dns server timeout Failed!')
            if i == 2:
                return False
        return True

    def run(self):
        """
        Run
        :return:
        """
        global task_flag
        logger.info(f'Version: {__version__}')
        logger.info('----------')
        logger.info(f'Start domain: {self.domain}')
        start_time = time.time()
        subs = self.load_sub_domain_dict()
        logger.info(f'Sub domain dict count: {len(subs)}')
        logger.info('Generate coroutines...')
        # Verify that all DNS server results are consistent
        stable_dns = []
        wildcard_ips = None
        last_dns = []
        only_similarity = False
        for dns in self.dns_servers:
            delay = self.check(dns)
            if not delay:
                logger.warning(f"@{dns} is not available, skip this DNS server")
                continue
            self.resolver = aiodns.DNSResolver(loop=self.loop, nameservers=[dns], timeout=self.resolve_timeout)
            job = self.query(self.wildcard_sub)
            sub, ret = self.loop.run_until_complete(job)
            logger.info(f'@{dns} {sub} {ret}')
            if ret is None:
                ret = None
            else:
                ret = sorted(ret)

            if dns in self.stable_dns_servers:
                wildcard_ips = ret
            stable_dns.append(ret)

            if ret:
                equal = [False for r in ret if r not in last_dns]
                if len(last_dns) != 0 and False in equal:
                    only_similarity = self.is_wildcard_domain = True
                    logger.info('Is a random resolve subdomain.')
                    break
                else:
                    last_dns = ret

        is_all_stable_dns = stable_dns.count(stable_dns[0]) == len(stable_dns)
        if not is_all_stable_dns:
            logger.info('Is all stable dns: NO, use the default dns server')
            self.resolver = aiodns.DNSResolver(loop=self.loop, nameservers=self.stable_dns_servers,
                                               timeout=self.resolve_timeout)
        # Wildcard domain
        is_wildcard_domain = not (stable_dns.count(None) == len(stable_dns))
        if is_wildcard_domain or self.is_wildcard_domain:
            if not self.skip_rsc:
                logger.info('This is a wildcard domain, will enumeration subdomains use by DNS+RSC.')
            else:
                logger.info(
                    'This is a wildcard domain, but it is --skip-rsc mode now, it will be drop all random resolve subdomains in results')
            self.is_wildcard_domain = True
            if wildcard_ips is not None:
                self.wildcard_ips = wildcard_ips
            else:
                self.wildcard_ips = stable_dns[0]
            logger.info(f'Wildcard IPS: {self.wildcard_ips}')
            if not self.skip_rsc:
                try:
                    self.wildcard_html = requests.get(
                        f'http://{self.wildcard_sub}.{self.domain}',
                        headers=self.request_headers, timeout=10, verify=False).text
                    self.wildcard_html = self.data_clean(self.wildcard_html)
                    self.wildcard_html_len = len(self.wildcard_html)
                    self.wildcard_html3 = requests.get(
                        f'http://{self.wildcard_sub3}.{self.domain}',
                        headers=self.request_headers, timeout=10, verify=False).text
                    self.wildcard_html3 = self.data_clean(self.wildcard_html3)
                    self.wildcard_html3_len = len(self.wildcard_html3)
                    logger.info(
                        f'Wildcard domain response html length: {self.wildcard_html_len} 3length: {self.wildcard_html3_len}')
                except requests.exceptions.SSLError:
                    logger.warning('SSL Certificate Error!')
                except requests.exceptions.ConnectTimeout:
                    logger.warning('Request response content failed, check network please!')
                except requests.exceptions.ReadTimeout:
                    self.wildcard_html = self.wildcard_html3 = ''
                    self.wildcard_html_len = self.wildcard_html3_len = 0
                    logger.warning(
                        f'Request response content timeout, {self.wildcard_sub}.{self.domain} and {self.wildcard_sub3}.{self.domain} maybe not a http service, content will be set to blank!')
                except requests.exceptions.ConnectionError:
                    logger.error('ESD can\'t get the response text so the rsc will be skipped. ')
                    self.skip_rsc = True
        else:
            logger.info('Not a wildcard domain')

        if not only_similarity:
            self.coroutine_count = self.coroutine_count_dns
            tasks = (self.query(sub) for sub in subs)
            self.loop.run_until_complete(self.start_e(tasks, len(subs)))
            logger.info(f"Brute Force subdomain count: {self.count}")
        dns_time = time.time()
        time_consume_dns = int(dns_time - start_time)
        logger.info(f'DNS query errors: {self.dns_query_errors}')

        # CA subdomain info
        ca_subdomains = []
        logger.info('Collect subdomains in CA...')
        ca_subdomains = CAInfo(self.domain).get_subdomains()
        if len(ca_subdomains):
            tasks = (self.query(sub) for sub in ca_subdomains)
            self.loop.run_until_complete(self.start_e(tasks, len(ca_subdomains)))
        logger.info(f'CA subdomain count: {len(ca_subdomains)}')

        # DNS Transfer Vulnerability
        transfer_info = []
        logger.info(f'Check DNS Transfer Vulnerability in {self.domain}')
        transfer_info = DNSTransfer(self.domain).transfer_info()
        if len(transfer_info):
            logger.warning(f'DNS Transfer Vulnerability found in {self.domain}!')
            tasks = (self.query(sub) for sub in transfer_info)
            self.loop.run_until_complete(self.start_e(tasks, len(transfer_info)))
        logger.info(f'DNS Transfer subdomain count: {len(transfer_info)}')

        total_subs = set(subs + transfer_info + ca_subdomains)

        # Use TXT,SOA,MX,AAAA record to find sub domains
        if self.multiresolve:
            logger.info('Enumerating subdomains with TXT, SOA, MX, AAAA record...')
            dnsquery = DNSQuery(self.domain, total_subs, self.domain)
            record_info = dnsquery.dns_query()
            tasks = (self.query(record[:record.find('.')]) for record in record_info)
            self.loop.run_until_complete(self.start_e(tasks, len(record_info)))
            logger.info(f'DNS record subdomain count: {len(record_info)}')

        if self.is_wildcard_domain and not self.skip_rsc:
            # Response similarity comparison
            total_subs = set(subs + transfer_info + ca_subdomains)
            self.wildcard_subs = list(set(subs).union(total_subs))
            logger.info(
                f'Enumerates {len(self.data)} sub domains by DNS mode in {str(datetime.timedelta(seconds=time_consume_dns))}')
            logger.info(
                f'Will continue to test the distinct({len(subs)}-{len(self.data)})={len(self.wildcard_subs)} domains used by RSC, the speed will be affected.')
            self.coroutine_count = self.coroutine_count_request
            self.remainder = len(self.wildcard_subs)
            tasks = (self.similarity(sub) for sub in self.wildcard_subs)
            self.loop.run_until_complete(self.start_e(tasks, len(self.wildcard_subs)))

            # Distinct last domains use RSC
            # Maybe misinformation
            # self.distinct()

            time_consume_request = int(time.time() - dns_time)
            logger.info(f'Requests time consume {str(datetime.timedelta(seconds=time_consume_request))}')
        # RS(redirect/response) domains
        while len(self.domains_rs) != 0:
            logger.info(f'RS(redirect/response) domains({len(self.domains_rs)})...')
            tasks = (self.similarity(''.join(domain.rsplit(self.domain, 1)).rstrip('.')) for domain in self.domains_rs)

            self.loop.run_until_complete(self.start_e(tasks, len(self.domains_rs)))

        # write output
        tmp_dir = self.project_directory + '/tmp'
        if not os.path.isdir(tmp_dir):
            os.mkdir(tmp_dir, 0o777)
        output_path_with_time = f'{tmp_dir}/.{self.domain}_{datetime.datetime.now().strftime("%Y-%m_%d_%H-%M")}.esd'
        output_path = f'{tmp_dir}/.{self.domain}.esd'
        if len(self.data):
            max_domain_len = max(map(len, self.data)) + 2
        else:
            max_domain_len = 2
        output_format = '%-{0}s%-s\n'.format(max_domain_len)
        with open(output_path_with_time, 'w') as opt, open(output_path, 'w') as op:
            for domain, ips in self.data.items():
                # The format is consistent with other scanners to ensure that they are
                # invoked at the same time without increasing the cost of
                # resolution
                if ips is None or len(ips) == 0:
                    ips_split = ''
                else:
                    ips_split = ','.join(ips)
                con = output_format % (domain, ips_split)
                op.write(con)
                opt.write(con)

        logger.info(f'Output: {output_path}')
        logger.info(f'Output with time: {output_path_with_time}')
        logger.info(f'Total domain: {len(self.data)}')
        time_consume = int(time.time() - start_time)
        logger.info(f'Time consume: {str(datetime.timedelta(seconds=time_consume))}')
        task_flag = True
        return self.data

class Heart(threading.Thread):
    
    def __init__(self):
        threading.Thread.__init__(self)
        
    
    def run(self):
        logger.info(f"心跳已启动")
        tmp_list = []
        while True:
            send_list = []
            for d_d_i in domain_domain_ips:
                if d_d_i not in tmp_list:
                    tmp_list.append(d_d_i)
                    send_list.append(d_d_i)
            logger.info(f"心跳 {send_list}")
            #跑完了 结束心跳
            if task_flag:
                logger.info(f"心跳结束")
                sys.exit(0)
            time.sleep(2)

def banner():
    print("""\033[94m
     ______    _____   _____  
    |  ____|  / ____| |  __ \ 
    | |__    | (___   | |  | |
    |  __|    \___ \  | |  | |
    | |____   ____) | | |__| |
    |______| |_____/  |_____/\033[0m\033[93m
    Enumeration Sub Domains v%s\033[92m
    """ % __version__)


def main():
    banner()
    parser = OptionParser(
        'Usage: esd -d feei.cn -F response_filter -p user:pass@host:port')
    parser.add_option('-d', '--domain', dest='domains', help='The domains that you want to enumerate')
    parser.add_option('-f', '--file', dest='input', help='Import domains from this file')
    parser.add_option('-F', '--filter', dest='filter', help='Response filter')
    parser.add_option('-s', '--skip-rsc', dest='skiprsc', help='Skip response similary compare', action='store_true',
                      default=False)
    parser.add_option('-S', '--split', dest='split', help='Split the dict into several parts', default='1/1')
    parser.add_option('-p', '--proxy', dest='proxy', help='Use socks5 proxy to access Google and Yahoo')
    parser.add_option('-m', '--multi-resolve', dest='multiresolve',
                      help='Use TXT, AAAA, MX, SOA record to find subdomains', action='store_true', default=False)
    (options, args) = parser.parse_args()

    domains = []
    response_filter = options.filter
    skip_rsc = options.skiprsc
    split_list = options.split.split('/')
    split = options.split
    multiresolve = options.multiresolve

    try:
        if len(split_list) != 2 or int(split_list[0]) > int(split_list[1]):
            logger.error('Invaild split parameter,can not split the dict')
            split = None
    except:
        logger.error(f'Split validation failed: {split_list}')
        exit(0)

    if options.proxy:
        proxy = {
            'http': 'socks5h://%s' % options.proxy,
            'https': 'socks5h://%s' % options.proxy
        }
    else:
        proxy = {}

    if options.domains is not None:
        for p in options.domains.split(','):
            p = p.strip().lower()
            re_domain = re.findall(r'^(([a-z0-9]+(-[a-z0-9]+)*\.)+[a-z]{2,})$', p)
            if len(re_domain) > 0 and re_domain[0][0] == p:
                domains.append(p.strip())
            else:
                logger.error(f'Domain validation failed: {p}')
    elif options.input and os.path.isfile(options.input):
        with open(options.input) as fh:
            for line_domain in fh:
                line_domain = line_domain.strip().lower()
                re_domain = re.findall(r'^(([a-z0-9]+(-[a-z0-9]+)*\.)+[a-z]{2,})$', line_domain)
                if len(re_domain) > 0 and re_domain[0][0] == line_domain:
                    domains.append(line_domain)
                else:
                    logger.error(f'Domain validation failed: {line_domain}')
    else:
        logger.error('Please input vaild parameter. ie: "esd -d feei.cn" or "esd -f /Users/root/domains.txt"')

    if 'esd' in os.environ:
        debug = os.environ['esd']
    else:
        debug = False
    logger.info(f'Debug: {debug}')
    logger.info(f'--skip-rsc: {skip_rsc}')

    logger.info(f'Total target domains: {len(domains)}')
    try:
        for d in domains:
            #esd = EnumSubDomain(d, response_filter, skip_rsc=skip_rsc, debug=debug, split=split,
            #                    proxy=proxy,
            #                    multiresolve=multiresolve)
            #esd.run()
            thread_esd = EnumSubDomain(d, response_filter, skip_rsc=skip_rsc, debug=debug, split=split,
                                proxy=proxy,
                                multiresolve=multiresolve)
            thread_heart = Heart()
            thread_esd.start()
            thread_heart.start()
            thread_esd.join()
            thread_heart.join()
    except KeyboardInterrupt:
        print('', end='\n')
        logger.info('Bye :)')
        exit(0)


if __name__ == '__main__':
    main()
