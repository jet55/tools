"""
This file is part of SDict Viewer (http://sdictviewer.sf.net) - 
a dictionary application that allows to use data bases 
in AXMASoft's open dictionary format. 

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 3 of the License.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.

Copyright (C) 2006-2007 Igor Tkach
"""
from __future__ import with_statement
import zlib
import bz2
from struct import unpack
import locale
import time
import marshal
import os
import os.path
from itertools import groupby
from Queue import Queue 
import util

settings_dir  = ".sdictviewer"
index_cache_dir = os.path.join(os.path.expanduser("~"),  settings_dir, "index_cache")
INDEXING_THRESHOLD = 1000

class GzipCompression:
    
    def __str__(self):
        return "gzip"
    
    def decompress(self, string):
        return zlib.decompress(string)
    
class Bzip2Compression:    
    
    def __str__(self):
        return "bzip2"
    
    def decompress(self, string):
        return bz2.decompress(string)
    
class NoCompression:
    
    def __str__(self):
        return "no compression"
        
    def decompress(self, string):
        return string
    
def read_raw(s, fe):
    return s[fe.offset:fe.offset + fe.length]

def read_str(s, fe):
    raw = read_raw(s, fe)
    return raw.replace('\x00', '');

def read_int(s, fe = None):      
    raw = read_raw(s, fe) if fe else s
    return unpack('<I', raw)[0]    

def read_short(raw):  
    return unpack('<H', raw)[0]    

def read_byte(raw):  
    return unpack('<B', raw)[0]    

class FormatElement:
    def __init__(self, offset, length, elementType = None):
        self.offset = offset
        self.length = length
        self.elementType = elementType

class Header:
                    
    f_signature = FormatElement(0x0, 4)
    f_input_lang = FormatElement(0x4, 3)
    f_output_lang = FormatElement(0x7, 3)
    f_compression = FormatElement(0xa, 1)
    f_num_of_words = FormatElement(0xb, 4)
    f_length_of_short_index=FormatElement(0xf, 4)
    f_title=FormatElement(0x13, 4)
    f_copyright=FormatElement(0x17, 4)
    f_version=FormatElement(0x1b, 4)
    f_short_index=FormatElement(0x1f, 4)
    f_full_index=FormatElement(0x23, 4)
    f_articles=FormatElement(0x27, 4)
                        
    def parse(self, str):
        self.signature = read_str(str, self.f_signature)
        if self.signature != 'sdct':
            raise DictFormatError, "Not a valid sdict dictionary"
        self.word_lang = read_str(str, self.f_input_lang)
        self.article_lang = read_str(str, self.f_output_lang)
        self.short_index_length = read_int(str, self.f_length_of_short_index)
        comp_and_index_levels_byte = read_byte(read_raw(str, self.f_compression)) 
        self.compressionType = comp_and_index_levels_byte & int("00001111", 2)
        self.short_index_depth = comp_and_index_levels_byte >> 4        
        self.num_of_words = read_int(str, self.f_num_of_words)
        self.title_offset = read_int(str, self.f_title)
        self.copyright_offset = read_int(str, self.f_copyright)
        self.version_offset = read_int(str, self.f_version)
        self.articles_offset = read_int(str, self.f_articles)
        self.short_index_offset = read_int(str, self.f_short_index)
        self.full_index_offset = read_int(str, self.f_full_index)
        
    
compressions = {0:NoCompression(), 1:GzipCompression(), 2:Bzip2Compression()}
        
class DictFormatError(Exception):
     def __init__(self, value):
         self.value = value
     def __str__(self):
         return repr(self.value)      

class LookupStoppedException(Exception):  
    pass

class WordLookup:
    def __init__(self, word, dict = None, article_ptr = None):
        self.word = word
        self.lookup = {}
        if dict and article_ptr:
            self.add_article(dict, article_ptr)
        
    def add_article(self, dict, article_ptr):
        self.lookup[dict] = article_ptr
        
    def add_articles(self, other):
        self.lookup.update(other.lookup)        
        
    def __str__(self):
        return self.word
    
    def read_articles(self):
        return [(dict,dict.read_article(article_ptr)) for dict, article_ptr in self.lookup.iteritems()]
        
class SkippedWord:
    def __init__(self, dict, word, full_index_ptr):
        self.dict = dict
        self.word = word
        self.full_index_ptr = full_index_ptr
        
    def __str__(self):
        return self.word +" [skipped]"
    
class SDictionary:         
    
    def __init__(self, file_name, encoding = "utf-8"):    
        self.encoding = encoding
        self.file_name = file_name
        self.file = open(file_name, "rb");
        self.header = Header()
        self.header.parse(self.file.read(43))  
        self.compression = compressions[self.header.compressionType]    
        self.title = self.read_unit(self.header.title_offset)  
        self.version = self.read_unit(self.header.version_offset)  
        self.copyright = self.read_unit(self.header.copyright_offset)
        self.index_cache_file_name = os.path.join(index_cache_dir, os.path.basename(self.file_name)+'-'+str(self.version)+".index")
        
    def __eq__(self, other):
        return self.key() == other.key()
    
    def __str__(self):
        return self.file_name
    
    def __hash__(self):
        return self.key().__hash__()

    def key(self):
        return (self.title, self.version, self.file_name)
        
    def read_unit(self, pos):
        f = self.file
        f.seek(pos);
        record_length= read_int(f.read(4))
        s = f.read(record_length)
        s = self.compression.decompress(s)
        return s

    def load(self):
        self.short_index = self.load_short_index()

    def load_short_index(self):
        #"try to read index from a cache, if that failes fall back to read_short_index(self)
        try:
            with open(self.index_cache_file_name, 'rb') as index_file:
                # check that the cached version matches the file we are reading
                read_title = marshal.load(index_file)
                read_version = marshal.load(index_file)
                if (read_title != self.title) or (read_version != self.version):
                    print "title or version missmatch in cached file"
                    raise ValueError
                short_index = marshal.load(index_file)
                #print "read cache from", self.index_cache_file_name
                return short_index
        except:
            print "could not read", self.index_cache_file_name
            pass
        short_index = self.read_short_index()
        return short_index

    def save_index(self):
        if not os.path.exists(index_cache_dir):
            os.makedirs(index_cache_dir)
        with open(self.index_cache_file_name, 'wb') as index_file:
            marshal.dump(self.title, index_file)
            marshal.dump(self.version, index_file)
            marshal.dump(self.short_index, index_file)
            print "wrote", self.index_cache_file_name


    def remove_index_cache_file(self):
        # should be done after the file is closed, to avoid raising an exception on windows
        try:
            os.remove(self.index_cache_file_name)
        except:
            print "could not remove", self.index_cache_file_name

    def read_short_index(self):        
        self.file.seek(self.header.short_index_offset)
        s_index_depth = self.header.short_index_depth
        index_entry_len = (s_index_depth+1)*4
        short_index_str = self.file.read(index_entry_len*self.header.short_index_length)
        short_index_str = self.compression.decompress(short_index_str)                
        index_length = self.header.short_index_length
        short_index = [{} for i in xrange(s_index_depth+2)]
        depth_range = xrange(s_index_depth)        
        for i in xrange(index_length):            
            entry_start = start_index = i*index_entry_len
            short_word = u''
            try:
                for j in depth_range:
                    #inlined unpack yields ~20% performance gain compared to calling read_int()
                    uchar_code =  unpack('<I',short_index_str[start_index:start_index+4])[0]
                    start_index+=4
                    if uchar_code == 0:
                        break
                    short_word += unichr(uchar_code)
            except ValueError, ve:
                # If Python is built without wide unicode support (which is the case on Maemo) 
                # it may not be possible to use some unicode chars. It seems best to ignore such index items. The rest of
                # the dictionary should be usable.
                print 'Failed to decode short index item ', i, ', will ignore: ', str(ve)                
                continue
            pointer_start = entry_start+s_index_depth*4
            pointer = unpack('<I',short_index_str[pointer_start:pointer_start+4])[0]  
            short_index[len(short_word)][short_word] = pointer        
        return short_index
       
    def get_search_pos_for(self, word):
        search_pos, starts_with = -1, None
        u_word = word.decode(self.encoding)
        for i in xrange(1, len(self.short_index)):
            index = self.short_index[i]    
            try:
                u_subword = u_word[:i]
                if index.has_key(u_subword):
                    search_pos = index[u_subword]
                    starts_with = u_subword.encode(self.encoding)
            except UnicodeDecodeError, ex:
                print ex            
        return search_pos, starts_with

    def get_word_list_iter(self, start_word):
        search_pos, starts_with = self.get_search_pos_for(start_word)
        #print "search_pos: %s, starts_with %s" % (search_pos, starts_with)
        if search_pos > -1:
            current_pos = self.header.full_index_offset
            read_item = self.read_full_index_item
            next_ptr = search_pos
            index_word = starts_with
            found = False 
            while index_word and index_word.startswith(starts_with):
                current_pos += next_ptr
                next_ptr, index_word, article_ptr = read_item(current_pos)
                if index_word.startswith(start_word):
                    found = True
                    yield WordLookup(index_word, self, article_ptr)
                yield SkippedWord(self, index_word, current_pos - self.header.full_index_offset)
            if not found:
                u_start_word = start_word.decode(self.encoding)
                self.ensure_index_depth(len(u_start_word))
                self.short_index[len(u_start_word)][u_start_word] = -1
                
    def index(self, items):
        if len(items) > INDEXING_THRESHOLD:
            t0 = time.time()
            items_to_index = [(i.word.decode(self.encoding), i.full_index_ptr) for i in items]
            for stats in self.do_index(items_to_index, self.header.short_index_depth + 1, INDEXING_THRESHOLD): yield stats
            print "[index] indexing %d items took %s s" % (len(items), time.time() - t0)
        
    def do_index(self, items, length, max_distance):
        #t0 = time.time()
        #print "[do_index] %s will index %d items with depth %d" % (str(self), len(items), length) 
        short_index = self.short_index
        self.ensure_index_depth(length)
        short_index_for_length = short_index[length]
        prev_word_start = None; last_index_point_index = 0; i = -1
        item_count = len(items)
        for word, current_pos in items:
            i += 1
            current_word_start =  word[:length]
            yield (length, i, item_count)
            #print "test: '%s'->'%s'" % (prev_word_start , current_word_start)
            if prev_word_start != current_word_start:
                #print "Adding index point '%s'" % current_word_start
                short_index_for_length[current_word_start] = current_pos
                if i - last_index_point_index > max_distance:
                    for stats in self.do_index(items[last_index_point_index:i], length + 1, max_distance): yield stats
                last_index_point_index = i     
            prev_word_start = current_word_start
        if item_count - 1 - last_index_point_index > max_distance:
            for stats in self.do_index(items[last_index_point_index:], length + 1, max_distance): yield stats
        #print "indexing %d items with depth %d took %s s" % (item_count, length, time.time() - t0)
    
    def ensure_index_depth(self, depth):
        while len(self.short_index) < depth + 1:
            self.short_index.append({})
                
    def read_full_index_item(self, pointer):
        try:
            f = self.file
            f.seek(pointer)
            s = f.read(8)
            next_word = unpack('<H', s[:2])[0]
            article_pointer = unpack('<I', s[4:])[0]
            word = f.read(next_word - 8) if next_word else None
            return next_word, word, article_pointer
        except Exception, e:
            if pointer >= self.header.articles_offset:
                print 'Warning: attempt to read word from illegal position in dict file'        
                return None
            print e
        
    def read_article(self, pointer):
        return self.read_unit(self.header.articles_offset + pointer)        
    
    def close(self, save_index = True):
        if save_index: self.save_index()
        self.file.close()        

class WordLookupByWord(dict):
    def __missing__(self, word):
        value = WordLookup(word)
        self.__setitem__(word, value)
        return value
        
class SDictionaryCollection:
    
    def __init__(self):
        self.dictionaries = util.ListMap()
    
    def add(self, dict):
        self.dictionaries[dict.header.word_lang].append(dict)
        
    def has(self, dict):
        lang_dicts = self.dictionaries[dict.header.word_lang]
        return lang_dicts.count(dict) == 1
    
    def remove(self, dict):        
        self.dictionaries[dict.header.word_lang].remove(dict)
        if len(self.dictionaries[dict.header.word_lang]) == 0:
            del self.dictionaries[dict.header.word_lang]
    
    def get_dicts(self, langs = None):
        dicts = []
        if langs:
            [dicts.extend(self.dictionaries[lang]) for lang in langs]
        else:
            [dicts.extend(list) for list in self.dictionaries.itervalues()]
        return dicts
    
    def langs(self):
        return self.dictionaries.keys()
    
    def get_word_list_iter(self, lang, start_word, max_from_one_dict = 20):
        for dict in self.dictionaries[lang]:
            count = 0
            for item in dict.get_word_list_iter(start_word):
                yield item
                count += (1 if isinstance(item, WordLookup) else 0)
                if count >= max_from_one_dict: break
    
    def stop_lookup(self):
        [dict.stop_lookup() for dict in self.get_dicts()]
        self.stopped = True
    
    def is_empty(self):
        return self.size() == 0
    
    def size(self):
        return len(self.get_dicts())        
        
        