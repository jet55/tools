import functools 
import re
import logging

from lxml import etree
import simplejson

from mwlib import uparser, xhtmlwriter
from mwlib.log import Log
Log.logfile = None

import mwaardwriter

tojson = functools.partial(simplejson.dumps, ensure_ascii=False)

NS = '{http://www.mediawiki.org/xml/export-0.3/}'

import multiprocessing
from multiprocessing import Pool, TimeoutError
from mwlib.cdbwiki import WikiDB

import mem

def convert(data):
    title, text, templatesdir = data
    templatedb = WikiDB(templatesdir) if templatesdir else None
    mwobject = uparser.parseString(title=title, 
                                   raw=text, 
                                   wikidb=templatedb)
    xhtmlwriter.preprocess(mwobject)
    text, tags = mwaardwriter.convert(mwobject)
    return title, tojson((text.rstrip(), tags))

def mem_check(rss_threshold=0, rsz_threshold=0, vsz_threshold=0):
    """
    Check memory usage for active child processes and return list of processes
    that exceed specified memory usage threshold (in megabytes). Threshold considered not set
    if it's value is 0 (which is default for all thresholds)
    """
    active = multiprocessing.active_children()
    logging.info('Checking memory usage (%d child processes), thresholds: rss %.1fM rsz %.1fM vsz %.1fM',
                 len(active), rss_threshold, rsz_threshold, vsz_threshold)
    processes = []
    for process in active:
        pid = process.pid
        logging.info('Checking memory usage for process %d', pid)
        rss = rsz = vsz = 0

        if 0 < rss_threshold:
            rss = mem.rss(pid) / 1024.0
            if rss_threshold <= rss:
                logging.warn('Process %d exceeded rss memory limit of %.1fM',
                             pid, rss_threshold)                
                processes.append(process)

        if 0 < rsz_threshold:
            rsz = mem.rsz(pid) / 1024.0
            if rsz_threshold <= rsz:
                logging.warn('Process %d exceeded rsz memory limit of %.1fM',
                             pid, rsz_threshold)                                
                processes.append(process)

        if 0 < vsz_threshold:
            vsz = mem.vsz(pid) / 1024.0
            if vsz_threshold <= vsz:
                logging.warn('Process %d exceeded vsz memory limit of %.1fM',
                             pid, vsz_threshold)                                
                processes.append(process)
                
        logging.info('Pid %d: rss %.1fM rsz %.1fM vsz %.1fM', pid, rss, rsz, vsz)
    return processes

class WikiParser():
    
    def __init__(self, options, consumer):
        self.templatedir = options.templates
        self.mem_check_freq = options.mem_check_freq
        self.consumer = consumer
        self.redirect_re = re.compile(r"\[\[(.*?)\]\]")
        self.article_count = 0
        self.processes = options.processes if options.processes else None 
        self.pool = None
        self.active_processes = multiprocessing.active_children()
        self.timeout = options.timeout         
        self.timedout_count = 0
        self.rss_threshold = options.rss_threshold
        self.rsz_threshold = options.rsz_threshold
        self.vsz_threshold = options.vsz_threshold
        
    def articles(self, f):
        for event, element in etree.iterparse(f):
            if element.tag == NS+'sitename':                
                self.consumer.add_metadata('title', element.text)
                element.clear()
                
            elif element.tag == NS+'base':
                m = re.compile(r"http://(.*?)\.wik").match(element.text)
                if m:
                    self.consumer.add_metadata("index_language", m.group(1))
                    self.consumer.add_metadata("article_language", m.group(1))
                                    
            elif element.tag == NS+'page':
                
                for child in element.iter(NS+'text'):
                    text = child.text
                
                if not text:
                    continue
                
                for child in element.iter(NS+'title'):
                    title = child.text
                    
                element.clear()

                if text.lstrip().lower().startswith("#redirect"): 
                    m = self.redirect_re.search(text)
                    if m:
                        redirect = m.group(1)
                        redirect = redirect.replace("_", " ")
                        meta = {u'redirect': redirect}
                        self.consumer.add_article(title, tojson(('', [], meta)))
                    continue
                logging.debug('Yielding "%s" for processing', title.encode('utf8'))                
                yield title, text, self.templatedir

    def reset_pool(self):
        if self.pool:
            logging.info('Terminating current worker pool')
            self.pool.terminate()
        logging.info('Creating new worker pool')
        self.pool = Pool(processes=self.processes)
        
    def parse(self, f):
        try:
            self.consumer.add_metadata('article_format', 'json')
            articles = self.articles(f)
            self.reset_pool()
            resulti = self.pool.imap_unordered(convert, articles)
            while True:                                                                                         
                try:                                                                                            
                    result = resulti.next(self.timeout)
                    title, serialized = result
                    self.consumer.add_article(title, serialized)
                    self.article_count += 1
                    if self.article_count % self.mem_check_freq == 0:                
                        processes = mem_check(rss_threshold=self.rss_threshold,
                                              rsz_threshold=self.rsz_threshold,
                                              vsz_threshold=self.vsz_threshold)
                        if processes:
                            logging.warn('%d process(es) exceeded memory limit, resetting worker pool', len (processes))
                            self.reset_pool()
                            resulti = self.pool.imap_unordered(convert, articles)
                except StopIteration:                                                                           
                    break            
                except TimeoutError:
                    self.timedout_count += 1
                    logging.error('Worker pool timed out (%d time(s) so far)', 
                                  self.timedout_count)
                    self.reset_pool()
                    resulti = self.pool.imap_unordered(convert, articles)
                    continue
                except KeyboardInterrupt:
                    logging.error('Keyboard interrupt: terminating worker pool')
                    self.pool.terminate()
                    raise
                    
            self.consumer.add_metadata("self.article_count", self.article_count)
        finally:
            self.pool.close()
            self.pool.join()
