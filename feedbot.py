#!/usr/bin/env python3

from datetime import datetime
import codecs
import configparser
import feedparser
import logging
import os
import re
import socket
import stat
import threading
import time
import urllib
from copy import copy
from bs4 import BeautifulSoup
from collections import namedtuple


socket.setdefaulttimeout(10)

INTERVAL = 30 # seconds between checking for new updates
MAX_LINE_LENGTH = 390

logger = logging.getLogger('feedbot')
logger.setLevel(logging.INFO)

class DefaultErrorHandler(urllib.request.HTTPDefaultErrorHandler):
	def http_error_default(self, req, fp, code, msg, headers):
		result = urllib.request.HTTPError(req.get_full_url(), code, msg, headers, fp)
		result.status = code
		return result


class Output:
	
	
	def __init__(self):
		self.socket_name = None
		self.file_name = None
		self.socket_handle = None
		self.file_handle = None
	
	
	def parse_config(self, section):
		
		if 'socket' in section:
			self.socket_name = section['socket']
		
		if 'file' in section:
			self.file_name = section['file']
	
	def send(self, message):
		
		# Open the IRC named pipe if it isn't open yet
		if self.socket_handle is None and self.socket_name is not None:
			try:
				self.socket_handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
				self.socket_handle.settimeout(1)
				self.socket_handle.connect(self.socket_name)
			except Exception as e:
				logger.error(u'Could not open socket "{0}": "{1}"'.format(self.socket_name, str(e)))
				self.socket_handle = False
		
		if self.socket_handle is not None and self.socket_handle is not False:
			try:
				self.socket_handle.send(message)
				return
			except Exception as e:
				logger.error(str(e))
				self.socket_handle = False
		
		if self.file_handle is None:
			self.file_handle = open(self.file_name, 'ab')
			try:
				os.chmod(self.file_name, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH)
			except Exception as e:
				logger.error(str(e))
		
		self.file_handle.write(message)
		self.file_handle.flush()
	
	def msg(self, recipient, message, ending = None):
		
		line = recipient + ' ' + message.replace('\n', ' ').replace('\r', '') + '\n'
		
		self.send(line.encode('utf-8'));
	
	def close(self):
		if self.socket_handle is not None and self.socket_handle is not False:
			self.socket_handle.close()
		self.socket_handle = None
		if self.file_handle is not None:
			self.file_handle.close()
			self.file_handle = None


class Feed:
	
	
	def __init__(self, output):
		self.output = output
		self.max_items = 5
		self.name = '(default)'
		self.url = None
		self.agent = 'FeedBot/1.0'
		self.interval = 0
		self.age = 0
		self.soup = None
		self.title_soup = None
		self.title_pattern = re.compile(r'(.*)')
		self.title_format = None
		self.link_soup = None
		self.link_pattern = re.compile(r'(.*)')
		self.link_format = None
		self.published_soup = None
		self.channels = set()
		self.debug_channels = set()
		self.old_items = None
		self.old_time = 0
		self.backoff = 0
		self.etag = None
		self.modified = None
		self.state = None
	
	
	def parse_config(self, section):
		
		if 'url' in section:
			self.url = section['url']
		
		if 'agent' in section:
			self.agent = section['agent']
		
		if 'interval' in section:
			self.interval = int(section['interval']) * 60
			self.age = self.interval + 1
		
		if 'soup' in section:
			self.soup = section['soup']
		
		if 'title_soup' in section:
			self.title_soup = section['title_soup']
		
		if 'title_pattern' in section:
			self.title_pattern = re.compile(section['title_pattern'])
		
		if 'title_format' in section:
			self.title_format = section['title_format']
		
		if 'link_soup' in section:
			self.link_soup = section['link_soup']
		
		if 'link_pattern' in section:
			self.link_pattern = re.compile(section['link_pattern'])
		
		if 'link_format' in section:
			self.link_format = section['link_format']
		
		if 'published_soup' in section:
			self.published_soup = section['published_soup']
		
		if 'channels' in section:
			self.channels = set(map(str.strip, section['channels'].split(',')))
		
		if 'debug_channels' in section:
			self.debug_channels = set(map(str.strip, section['debug_channels'].split(',')))
		
		if 'state' in section:
			self.state = section['state']
		
		if 'max_items' in section:
			self.max_items = int(section['max_items'])
	
	
	def state_file(self):
		return self.state + '/' + self.name
	
	
	def load(self):
		
		if not self.url:
			raise RuntimeError(u'Missing rss url for feed {0}'.format(self.name))
		
		if self.interval < 0:
			raise RuntimeError(
				u'Invalid rss update interval {0} for feed {1}'.format(self.interval, self.name))
		
		if self.soup and (not self.title_soup) and (not self.link_soup):
			raise RuntimeError(
				u'soup requires title_soup and/or link_soup for feed {1}'.format(self.name))
		
		if os.path.exists(self.state_file()):
			old_items = set()
			handle = codecs.open(self.state_file(), 'r', encoding='utf-8')
			try:
				first = True
				for line in handle:
					line = line.rstrip()
					if line:
						if first:
							first = False
							self.old_time = float(line)
						else:
							old_items.add(str(line))
				if not first:
					self.old_items = old_items
				
			finally:
				handle.close()
	
	
	def save(self):
		if self.old_items is not None:
			handle = codecs.open(self.state_file(), 'w', encoding='utf-8')
			try:
				handle.write(str(self.old_time) + u'\n')
				for guid in self.old_items:
					handle.write(guid + u'\n')
			finally:
				handle.close()
	
	
	def disable(self, message):
		message = u'{0}: Can\'t parse feed, disabling: {1}'.format(self.name, message)
		logger.warning(message)
		if self.backoff != 0:
			for channel in self.debug_channels:
				self.output.msg(channel, message)
		self.backoff += self.interval + (self.backoff / 10)
	
	
	def msg(self, message):
		for channel in self.channels:
			self.output.msg(channel, message)
	
	
	def new_item(self, item):
		
		title = item.title if 'title' in item else '';
		if self.title_format:
			title = self.title_pattern.sub(self.title_format, title)
		
		max_length = MAX_LINE_LENGTH
		link = ''
		if 'link' in item:
			link = item.link
			if self.link_format:
				link = self.link_pattern.sub(self.link_format, link)
			if link and title:
				link = ' - ' + link
			max_length -= len(link)
		
		if title and len(title) > max_length:
			title = title[:max_length - 2] + ' …'
		
		message = title + link
		if message:
			self.msg(message)
	
	
	@staticmethod
	def guid(item):
		if 'guid' in item:
			return str(item.guid.strip().replace('\n',' '))
		guid = ''
		if 'title' in item:
			guid = item.title;
		if 'link' in item:
			guid = item.link;
		if 'published' in item:
			guid += '#' + item.published
		return str(guid.strip().replace('\n',' '))
	
	def update_feed(self):
		
		mtime = None
		if self.url.find(':') == -1:
			mtime = os.path.getmtime(self.url)
			if self.modified and self.modified >= mtime:
				Status = namedtuple('Status', 'status')
				return Status(304)
		
		fp = feedparser.parse(self.url, etag=self.etag, modified=self.modified, agent=self.agent)
		
		# Check for malformed XML
		if fp.bozo and not isinstance(fp.bozo_exception, feedparser.CharacterEncodingOverride):
			raise fp.bozo_exception
		
		# Check HTTP status
		if getattr(fp, 'status', 200) == 410: # GONE
			raise urllib.request.HTTPError(self.url, 410, 'Gone.', { }, fp)
		
		if mtime:
			setattr(fp, 'modified', mtime)
		
		return fp
	
	@staticmethod
	def get_text(blob):
		try:
			return blob.get_text().strip()
		except:
			return blob.strip()
	
	def update_soup(self):
		
		request = urllib.request.Request(self.url)
		
		request.add_header('User-Agent', self.agent)
		
		if self.etag and not self.modified:
			request.add_header('If-None-Match', self.etag)
		
		if self.modified:
			request.add_header('If-Modified-Since', self.modified)
		
		opener = urllib.request.build_opener(DefaultErrorHandler())
		
		fp = opener.open(request)
		
		setattr(fp, 'entries', None)
		
		if hasattr(fp, 'status'):
			if fp.status == 304 or fp.status == 301:
				return
			if fp.status != 200:
				raise fp
		
		page = BeautifulSoup(fp.read(), features='lxml')
		
		entries = [ ]
		
		for post in eval(self.soup, {}, {"page" : page}):
			
			try:
				entry = feedparser.util.FeedParserDict()
			except:
				entry = feedparser.FeedParserDict()
			
			if self.title_soup:
				entry['title'] = self.get_text(eval(self.title_soup, {}, {"post" : post}))
			
			if self.link_soup:
				url = self.get_text(eval(self.link_soup, {}, {"post" : post}))
				entry['link'] = urllib.parse.urljoin(self.url, url)
			
			if self.published_soup:
				entry['published'] = self.get_text(eval(self.published_soup, {}, {"post" : post}))
			
			entries.append(entry)
		
		setattr(fp, 'entries', entries)
		
		etag = fp.headers.get('ETag')
		if etag:
			setattr(fp, 'etag', etag)
		
		modified = fp.headers.get('Last-Modified')
		if modified:
			setattr(fp, 'modified', modified)
		
		return fp
	
	def update(self, elapsed_seconds):
		
		# Support per-feed update interval
		self.age += elapsed_seconds
		if self.age < self.interval + self.backoff:
			return False
		self.age = self.age % self.interval
		
		# Download feed snapshot
		try:
			if self.soup:
				fp = self.update_soup()
			else:
				fp = self.update_feed()
		except FileNotFoundError as e:
			self.disable(str(e))
			return True
		except urllib.request.HTTPError as e:
			self.disable(str(e))
			return True
		except IOError as e:
			self.disable(str(e))
			return True
		except Exception as e:
			self.disable(str(e))
			return True
		
		# fp.status will only exist if pulling from an online feed
		status = getattr(fp, 'status', 200)
		
		# Check HTTP status
		if status == 301 and hasattr(fp, 'href'): # MOVED_PERMANENTLY
			logger.warning(u'{0}: status = 301 (Moved Permanently), updating URI to {1}'.format(
				self.name, fp.href))
			self.url = fp.href
		if status == 304: # NOT MODIFIED
			logger.info(u'{0}: status = 304 (Not Modified)'.format(self.name))
			self.backoff = 0
			return True
		
		# Check if anything changed
		new_etag = fp.etag if hasattr(fp, 'etag') else None
		if new_etag is not None and new_etag == self.etag:
			logger.info(u'{0}: Same etag: {1}'.format(self.name, new_etag))
			self.backoff = 0
			return True
		new_modified = fp.modified if hasattr(fp, 'modified') else None
		if new_modified is not None and new_modified == self.modified:
			logger.info(u'{0}: Same modification time: {1}'.format(
				self.name, new_modified))
			self.backoff = 0
			return True
		
		if len(fp.entries) == 0:
			self.disable(u'no items matched')
			return True
		
		self.backoff = 0
		
		logger.info(u'{0}: status = {1}, items = {2}, etag = {3}, time = {4}'.format(
			self.name, status, len(fp.entries), new_etag, new_modified))
		
		# Check for new items
		new_items = (self.old_items is None)
		if fp.entries and self.old_items is not None:
			skipped = -self.max_items
			for item in reversed(fp.entries):
				guid = self.guid(item)
				if guid in self.old_items:
					continue
				new_items = True
				if 'published_parsed' in item:
					new_time = time.mktime(item.published_parsed)
					if new_time <= self.old_time:
						logger.warning(u'{0}: Old ptime: {1} <= {2} "{3}"'.format(
							self.name, new_time, self.old_time, guid))
						continue
				if skipped < 0:
					logger.info(u'{0}: New item: "{1}"'.format(self.name, guid))
					self.new_item(item)
				skipped += 1
			if skipped == 1:
				self.msg(u'(and one more item)')
			elif skipped > 0:
				self.msg(u'(and {0} more items)'.format(skipped))
		
		# Update the known items list
		if new_items:
			if self.old_items is None:
				self.old_items = set()
			for item in reversed(fp.entries):
				self.old_items.add(self.guid(item))
			if fp.entries and 'published_parsed' in fp.entries[0]:
				self.old_time = max(self.old_time, time.mktime(fp.entries[0].published_parsed))
			self.save()
		
		# Update the last update time
		self.etag = new_etag
		self.modified = new_modified
		
		return True

# create file handler which logs even debug messages

# create console handler with a higher log level
logger.addHandler(logging.StreamHandler())

config = configparser.ConfigParser()
config.sections()
config.read('feedbot.ini')
config.read('feedbot.private.ini')

if 'log' in config['DEFAULT']:
	logger.addHandler(logging.FileHandler(config['DEFAULT']['log']))

output = Output()
output.parse_config(config['DEFAULT'])

defaults = Feed(output)
defaults.parse_config(config['DEFAULT'])

feeds = [ ]
for name in config.sections():
	
	feed = copy(defaults)
	feed.name = name
	feed.parse_config(config[name])
	
	feeds.append(feed)

for feed in feeds:
	feed.load()
	logger.info(u'{0}: {1} {2} @{3} #={4} >={5}'.format(
		u'Soup' if feed.soup else u'Feed',
		feed.name, feed.url, feed.interval,
		len(feed.old_items) if feed.old_items is not None else None, feed.old_time))

next = 0
while True:
	
	try:
		for i in [(i + next) % len(feeds) for i in range(len(feeds))]:
			next = i + 1
			if feeds[i].update(INTERVAL):
				break
			output.close()
	except Exception as e:
		logger.error(str(e))
	finally:
		output.close()
	
	time.sleep(INTERVAL)
