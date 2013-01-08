#!/usr/bin/env python
"""
Helper functions for sam format, most importantly [m]pileup

Should be replaced with PySam in the future once mpileup and all its
options are supported fully (e.g. BAQ, depth etc)
"""



# Copyright (C) 2011, 2012 Genome Institute of Singapore
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.




#--- standard library imports
#
import subprocess
import logging
import re
import copy
import os

#--- third-party imports
#
# /

#--- project specific imports
#
from lofreq import utils
from lofreq import conf
from lofreq_ext import depth_stats

__author__ = "Andreas Wilm"
__email__ = "wilma@gis.a-star.edu.sg"
__copyright__ = "2011, 2012 Genome Institute of Singapore"
__license__ = "GPL2"


#global logger
# http://docs.python.org/library/logging.html
LOG = logging.getLogger("")
logging.basicConfig(level=logging.WARN,
                    format='%(levelname)s [%(asctime)s]: %(message)s')


VALID_BASES = ['A', 'C', 'G', 'T', 'N']
        


class PileupColumn():
    """
    Pileup column class. Parses samtools m/pileup output.

    See http://samtools.sourceforge.net/pileup.shtml

    Using namedtuples seemed too inflexible:
    namedtuple('PileupColumn', 'chrom coord ref_base coverage read_bases base_quals')
    pileup_column = PileupColumn(*(line.split('\t')))
    pileup_column = PileupColumn._make((line.split('\t')))
    """


    def __init__(self, line=None):
        """
        """
        # chromosome name
        self.chrom = None
        
        # 0-based coordinate (in: 1-based coordinate)
        self.coord = None
        
        # reference base
        self.ref_base = None

        # locally determined consensus base
        self.cons_base = None
        
        # the number of reads covering the site
        self.coverage = None

        # only VALID_BASES allowed
        self._bases_and_quals = dict()
        for b in VALID_BASES:
            self._bases_and_quals[b.upper()] = dict()
            self._bases_and_quals[b.lower()] = dict()

        self.num_ins_events = 0
        self.avg_ins_len = 0
        self.num_del_events = 0
        self.avg_del_len = 0
        self.num_read_starts = 0
        self.num_read_ends = 0

        if line:
            self.parse_line(line)


            
    def determine_cons(self):
        """
        Quality aware determination of consensus
        """

        # convert base-call qualities to probabilites/freqs (ignoring
        # Q2) to determine consensus.
        
        base_qual_hist = self.get_base_and_qual_hist(
            keep_strand_info=False)        

        base_probsum = dict() # actually sums of probs
        for base in base_qual_hist.keys():
            if base == 'N':
                continue
            assert base in 'ACGT', (
                "Only allowed bases/keys are A, C, G or T, but not %s" % base)

            probsum = 0
            for (qual, count) in base_qual_hist[base].iteritems():
                if qual <= 2: # 2 is not a quality
                    continue
                # A base-call with Q=20, means error-prob=0.01 means
                # prob=1-0.01=0.99
                probsum += count * (1.0 - utils.phredqual_to_prob(qual))

            base_probsum[base] = probsum

        # sort in ascending order                                                                                                                                               
        sorted_probsum = sorted(base_probsum.items(), 
                                key=lambda x: x[1])
        if sorted_probsum[-1][1] - sorted_probsum[-2][1] < 0.000001:
            # cons is N if tied
            cons_base = 'N'
        else:
            # return "true" consensus                                                                                                                                   
            cons_base = sorted_probsum[-1][0]
                                                                
        #LOG.debug("cons_base=%s base_qual_hist=%s base_probsum=%s" % (
        #    cons_base, base_qual_hist, base_probsum))
                       
        return cons_base

        
    def parse_line(self, line):
        """Split a line of pileup output and set values accordingly

        From http://samtools.sourceforge.net/pileup.shtml:
        
        At the read base column, a dot stands for a match to the
        reference base on the forward strand, a comma for a match on
        the reverse strand, `ACGTN' for a mismatch on the forward
        strand and `acgtn' for a mismatch on the reverse strand. A
        pattern `\+[0-9]+[ACGTNacgtn]+' indicates there is an
        insertion between this reference position and the next
        reference position. The length of the insertion is given by
        the integer in the pattern, followed by the inserted sequence.
        Here is an example of 2bp insertions on three reads:

        seq2 156 A 11  .$......+2AG.+2AG.+2AGGG    <975;:<<<<<


        Similarly, a pattern `-[0-9]+[ACGTNacgtn]+' represents a
        deletion from the reference. Here is an exmaple of a 4bp
        deletions from the reference, supported by two reads:

        seq3 200 A 20 ,,,,,..,.-4CACC.-4CACC....,.,,.^~. ==<<<<<<<<<<<::<;2<<

        Also at the read base column, a symbol `^' marks the start of
        a read segment which is a contiguous subsequence on the read
        separated by `N/S/H' CIGAR operations. The ASCII of the
        character following `^' minus 33 gives the mapping quality. A
        symbol `$' marks the end of a read segment. Start and end
        markers of a read are largely inspired by Phil Green's CALF
        format. These markers make it possible to reconstruct the read
        sequences from pileup. SAMtools can optionally append mapping
        qualities to each line of the output. This makes the output
        much larger, but is necessary when a subset of sites are
        selected.
        """

        assert self.coord == None, (
            "Seems like I already read some values")

        line_split = line.split('\t')
        assert len(line_split) == 6, (
            "Couldn't parse pileup line: '%s'" % line)

        self.chrom = line_split[0]
        # in: 1-based coordinate
        self.coord = int(line_split[1]) - 1
        self.ref_base = line_split[2].upper() # paranoia upper()
        self.coverage = int(line_split[3])

        bases = line_split[4]

        # convert quals immediately to phred scale
        quals = [ord(c)-33 for c in line_split[5]]
        if not all([q>=0 and q<100 for q in quals]):
            LOG.warn("Some base qualities out of valid range for %s at %d: %s" % (
                self.chrom, self.coord+1, [q for q in quals if q<0 or q>100]))
        
        # convert special reference markup to actual reference
        bases = bases.replace(".", self.ref_base.upper())
        bases = bases.replace(",", self.ref_base.lower())

        # NOTE: we are not using start/end info, so delete it to avoid
        # confusion. we are not using indel info, so delete it to avoid
        # confusion. deletion on reference ('*') have qualities which
        # will be deleted as well.
        bases = self.rem_startend_markup(bases)
        (bases, quals) = self.rem_indel_markup(bases, quals)
        assert len(bases) == len(quals), (
            "Mismatch between number of parsed bases and"
            " quality values at %s:%d\n" % (self.chrom, self.coord+1))

        if len(bases) != self.coverage-self.num_del_events:
            LOG.warn("Mismatch between number of bases (= %d) and"
                     " samtools coverage value (= %d)."
                     " Ins/del events: %d/%d. Cleaned base_str is '%s'."
                     " Line was '%s'" % (
                         len(bases), self.coverage, self.num_ins_events, 
                         self.num_del_events, bases, line))

        for (i, b) in enumerate(bases):
            # paranoia. have seen gaps in pileup
            if b.upper() not in VALID_BASES: 
                continue
            q = quals[i]
            self._bases_and_quals[b][q] = self._bases_and_quals[b].get(q, 0) + 1

        cons_base = self.determine_cons()
        if cons_base == '-' or cons_base == 'N':
            cons_base = self.ref_base
        self.cons_base = cons_base

        
            
    def rem_startend_markup(self, bases_str):
        """
        Remove end and start (incl mapping) markup from read bases string

        From http://samtools.sourceforge.net/pileup.shtml:

        ...at the read base column, a symbol `^' marks the start of a
        read segment which is a contiguous subsequence on the read
        separated by `N/S/H' CIGAR operations. The ASCII of the
        character following `^' minus 33 gives the mapping quality. A
        symbol `$' marks the end of a read segment.
        """
        
        org_len = len(bases_str)
        bases_str = bases_str.replace('$', '')
        self.num_read_ends = org_len-len(bases_str)

        org_len = len(bases_str)
        bases_str = re.sub('\^.', '', bases_str)
        # we delete the insertion markup plus the inserted base,
        # therefore divide by two
        self.num_read_starts = (org_len-len(bases_str))/2

        
        return bases_str

    
    def rem_indel_markup(self, bases_str, quals):
        """
        Remove indel markup from read bases string

        From http://samtools.sourceforge.net/pileup.shtml:

        A pattern '\+[0-9]+[ACGTNacgtn]+' indicates there is an
        insertion between this reference position and the next
        reference position. Similarly, a pattern
        '-[0-9]+[ACGTNacgtn]+' represents a deletion from the
        reference. The deleted bases will be presented as '*' in the
        following lines.


        FIXME: this is slow (makes up a fifth of the cummulative time of
        parse_line)
        """
       
        # First the initial +- markup for which no quality value
        # exists: find out how many insertion/deletions happened
        # first, so that you can then delete the right amount of
        # nucleotides afterwards.
        #
        while True:
            match = re.search('[-+][0-9]+', bases_str)
            if not match:
                break

            if bases_str[match.start()] == '+':
                self.num_ins_events += 1
            else:
                assert bases_str[match.start()] == '-'

            num = int(bases_str[match.start()+1:match.end()])
            left = bases_str[:match.start()]
            right = bases_str[match.end()+num:]
            bases_str = left + right


        # now delete the deletion on the reference marked as stars
        # (which have quality values; see also
        # http://seqanswers.com/forums/showthread.php?t=3388)
        # and return

        self.num_del_events = bases_str.count('*')

        quals = [(q) for (b, q) in zip(bases_str, quals)
                      if b != '*']
        bases_str = ''.join([b for b in bases_str if b != '*'])

        return (bases_str, quals)


    def get_counts_for_base(self, base, min_qual=3, keep_strand_info=True):
        """Count base (summarise histograms) and return as fw, rv
        count dict. If keep_strand_info is false, then counts are
        returned as sum of fw and rv
        """


        if not self._bases_and_quals.has_key(base):
            if keep_strand_info:
                return (0, 0)
            else:
                return 0
        
        fw_count = 0
        b = base.upper()
        fw_count += sum([c for (q, c) in self._bases_and_quals[b].iteritems()
                      if q >= min_qual])

        rv_count = 0
        b = base.lower()
        rv_count += sum([c for (q, c) in self._bases_and_quals[b].iteritems()
                      if q >= min_qual])
        
        if keep_strand_info:
            return (fw_count, rv_count)
        else:
            return sum([fw_count, rv_count])


    def get_all_base_counts(self, min_qual=3, keep_strand_info=True):
        """Frontend to get_counts_for_base: Count bases (summarise
        histograms) and return as dict with (uppercase) bases as keys.
        Values will be an int (sum of fw and rv) unless
        keep_strand_info is False (returns sum of both)
        """

        base_counts = dict()
        for base in VALID_BASES:
            base_counts[base] = self.get_counts_for_base(
                base, min_qual, keep_strand_info)
    
        return base_counts


    def get_base_and_qual_hist(self, keep_strand_info=True):
        """Return a copy of base/quality histograms. If
        keep_strand_info is False, then only uppercase bases will be
        used as keys and values are counts summarised for fw and rv
        strand
        """
        
        if keep_strand_info:
            return copy.deepcopy(self._bases_and_quals)

        # a bit more tricky...
        base_and_qual_hists = dict()
        for base in self._bases_and_quals:
            # don't merge twice
            if base.islower():
                continue
                
            # like dict.update() but add instead of replace
            fw_dict = self._bases_and_quals[base.upper()]
            rv_dict = self._bases_and_quals[base.lower()]
            qual_union = set(fw_dict.keys() + rv_dict.keys())
            base_and_qual_hists[base] = dict(
                [(q, fw_dict.get(q, 0) + rv_dict.get(q, 0))
                 for q in qual_union])

        return base_and_qual_hists

    
def tokenizer(s, c):
    """From http://stackoverflow.com/questions/4586026/splitting-a-string-into-an-iterator"""
    i = 0
    while True:
        try:
            j = s.index(c, i)
        except ValueError:
            yield s[i:]
            return
        yield s[i:j]
        i = j + 1

         
class LoFreqPileupColumn(PileupColumn):
    """lofreq_samtools specific pileup parser"""

    
    def parse_line(self, line):
        """Overwriting PileupColumn.parse_line() with a version that
        parses lofreq_samtools mpileup output"""

        line = line.rstrip()
        if len(line) == 0:
            raise ValueError, ("Empty pileup line detected")
        
        for (field_no, field_val) in enumerate(tokenizer(line, '\t')):
            if field_no == 0:
                self.chrom = field_val
            elif field_no == 1:
                # in: 1-based coordinate
                self.coord = int(field_val) - 1
            elif field_no == 2:
                self.ref_base = field_val.upper() # paranoia upper()
            elif field_no == 3:
                self.coverage = int(field_val)
            elif field_no == 4:
                for p in xrange(0, len(field_val), 2):
                    (b, q) = field_val[p:p+2]
                    # convert quals to phred scale
                    q = ord(q)-33
                    self._bases_and_quals[b][q] = self._bases_and_quals[b].get(q, 0) + 1
                    
                # NOTE: this assumes there is no filtering done on the pileup level
                num_bq = sum(sum(self._bases_and_quals[b].values()) 
                             for b in self._bases_and_quals.keys())
                assert self.coverage >= num_bq , (
                    "Pileup parsing error: (raw) coverage (%d) smaller"
                    " than number of bases (%d)" % (num_bq, self.coverage))
                
                for b in self._bases_and_quals.keys():
                    assert b.upper() in VALID_BASES
                
            elif field_no == 5:
                # Example: 
                # heads=0 #tails=13 #ins=0 ins_len=0.0 #del=0 del_len=0.0
                field_dict = dict([x.split('=') 
                                   for x in field_val.split(' ')])
                try:
                    self.num_read_starts = int(field_dict['#heads'])
                    self.num_read_ends = int(field_dict['#tails'])
                    self.num_ins_events = int(field_dict['#ins'])
                    self.avg_ins_len = float(field_dict['ins_len'])
                    self.num_del_events = int(field_dict['#del'])
                    self.avg_del_len = float(field_dict['del_len'])
                except KeyError:
                    "Couldn't parse indel markup from pileup"
                    "  (which was '%s')" % (field_val)
                    raise
            else:
                LOG.warn("More fields than expected in pileup line."
                         " Will try to continue anyway. Line was '%s'" % line)

        cons_base = self.determine_cons()
        if cons_base == '-' or cons_base == 'N':
            cons_base = self.ref_base
        self.cons_base = cons_base
        
    

class Pileup(object):
    """Frontend to samtools mpileup
    """
    
    def __init__(self, bam, ref_fa=None, samtools=conf.SAMTOOLS):
        """init
        """

        self.bam = bam
        self.ref_fa = ref_fa
        self.samtools = samtools
        self.samtools_version = None        
        self.samtools_version = samtools_version(samtools)
        if self.samtools_version and self.samtools_version < (0, 1, 13):
            LOG.warn("Your samtools installation looks too old."
                     " Will try to continue anyway")

        
    
    def generate_pileup(self, baq='extended', max_depth=100000,
                        region_bed=None, join_mapq_and_baseq=False):
        """Pileup line generator
        """
        
        cmd_list = [self.samtools, 'mpileup']
        
        cmd_list.extend(['-d', "%d" % max_depth])
        
        if baq == 'off':
            cmd_list.append('-B')
        elif baq == 'extended':
            cmd_list.append('-E')
        elif baq != 'on':
            raise ValueError, ("Unknown BAQ option '%s'" % (baq))        
        
        if join_mapq_and_baseq:
            cmd_list.append('-j')
            

        if region_bed:
            cmd_list.extend(['-l', region_bed])

        if self.ref_fa:
            cmd_list.extend(['-f', self.ref_fa])
                    
        cmd_list.append(self.bam)
            
        LOG.info("Executing %s" % (cmd_list))
        try:
            p = subprocess.Popen(cmd_list, 
                                 stdout=subprocess.PIPE, 
                                 stderr=subprocess.PIPE)
        except:
            LOG.fatal("The following command failed: %s" % (
                ' '.join(cmd_list)))
            raise
        for line in p.stdout:
            if self.samtools.endswith("lofreq_samtools"):
                yield LoFreqPileupColumn(line)
            else:
                yield PileupColumn(line)

        
    
def sq_list_from_header(header):
    """
    Parse sequence name/s from header. Will return a list. Not sure if
    several names are allowed.
    """

    sq_list = []
    for line in header:
        line_split = line.split()
        try:
            if line_split[0] != "@SQ":
                continue
            if line_split[1].startswith("SN:"):
                sq_list.append(line_split[1][3:])
        except IndexError:
            continue
    return sq_list



def len_for_sq(header, sq):
    """
    Parse sequence length from header.
    """

    for line in header:
        line_split = line.split('\t')
        try:
            if line_split[0] != "@SQ":
                continue
            if not line_split[1].startswith("SN:"):
                continue
            if not line_split[1][3:] == sq:
                continue

            # right line, but which is the right field?
            for field in line_split[2:]:
                if field.startswith("LN:"):
                    return int(field[3:])
        except IndexError:
            continue
    return None

        
            
    
def sam_header(fbam, samtools=conf.SAMTOOLS):
    """
    Calls 'samtools -H view', parse output and return
    
    Arguments:
    - fbam:
    is the bam file to parse
    - samtools_binary:
    samtools binary name/path
    
    Results:
    Returns the raw header as list of lines
    
    NOTE:
    Results might change or exeuction fail depending on used samtools version.
    Tested on Version: 0.1.13 (r926:134)
    """
    
    cmd = '%s view -H %s' % (
        samtools, fbam)

    # http://samtools.sourceforge.net/pileup.shtml
    LOG.debug("calling: %s" % (cmd))
    # WARNING: "The data read is buffered in memory, so do not use
    # this method if the data size is large or unlimited." Only other
    # option I see is to write to file.
    process = subprocess.Popen(cmd.split(),
                               shell=False,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    (stdoutdata, stderrdata) =  process.communicate()

    retcode = process.returncode
    if retcode != 0:
        LOG.fatal("%s exited with error code '%d'." \
                  " Command was '%s'. stderr was: '%s'" % (
                      cmd.split()[0], retcode, cmd, stderrdata))
        raise OSError
                       
    for line in str.splitlines(stderrdata):
        if not len(line):
            continue
        if line == "[mpileup] 1 samples in 1 input files":
            continue
        if line == "[fai_load] build FASTA index.":
            continue
        else:
            LOG.warn("Unhandled line on stderr detected: %s" % (line))

    return str.splitlines(stdoutdata)


def samtools_version(samtools):
    """Returns samtools version as a tuples of major-, minor-version and
    patch-level
    """
    
    try:
        p = subprocess.Popen([samtools], 
                             stdout=subprocess.PIPE, 
                             stderr=subprocess.PIPE)
    except OSError:
        LOG.error("The following command failed: %s" % (
            samtools))
        raise
    
    lines = p.stderr.readlines()
    try:
        version_line = [l for l in  lines if "Version:" in l][0]
        version_str = version_line.split()[1]
        (majorv, minorv, patchlevel) = [
            int(x) for x in version_str.split(".")]            
    except IndexError:
        LOG.warn("Couldn't determine samtools version")
        return None
    
    return (majorv, minorv, patchlevel)


def sum_chrom_len(fbam, chrom_list=None):
    """
    Return length of all chromsomes. If chrom_list is not empty then
    only consider those. Length is extracted from BAM file (fbam)
    header
    """
    
    sum_sq_len = 0
    header = sam_header(fbam)
    if header == False:
        LOG.critical("samtools header parsing failed test")
        raise ValueError
    sq = sq_list_from_header(header)

    # use all if not set
    if not chrom_list:
        chrom_list = sq
        
    for chrom in chrom_list:
        assert chrom in sq, (
        "Couldn't find chromosome '%s' in BAM file '%s'" % (
            chrom, fbam))
        
        sq_len = len_for_sq(header, chrom)
        LOG.info("Adding length %d for chrom %s" % (sq_len, chrom))
        sum_sq_len += sq_len
        
    return sum_sq_len


def auto_bonf_factor(bam, bed_file=None, excl_file=None, chrom=None):
    """Automatically determine Bonferroni factor to use for SNV
    predictions on BAM file. Note: excl-file/chrom are deprecated. Use
    bed-file instead"""

    if excl_file or chrom:
        assert excl_file and chrom, (
            "If exclude file is given I also need a chromosome, vice versa")

    if bed_file:
        assert not excl_file, ("Can only use either bed or excl-file")

    # exclude positions
    #
    if excl_file:
        excl_pos = []
        excl_pos = utils.read_exclude_pos_file(excl_file)
        LOG.info("Parsed %d positions from %s" % (
            len(excl_pos), excl_file))
        
        sum_sq_len = sum_chrom_len(bam, [chrom])
        sum_sq_len -= len(excl_pos)

    elif bed_file:

        bed_coords = utils.read_bed_coords(bed_file)
        sum_sq_len = 0
        for (chrom, ranges) in bed_coords.iteritems():
            for r in ranges:
                LOG.debug("bed coord range for %s: %d-%d" % (
                    chrom, r[0], r[1]))
                diff = r[1]-r[0]
                assert diff > 0
                sum_sq_len += diff
    else:
        # look at all
        sum_sq_len = sum_chrom_len(bam)

    bonf_factor = sum_sq_len * 3
    return bonf_factor



def __auto_bonf_factor_from_depth(bam, bed_file=None,
                                min_base_q=3, min_map_q=0, samtools=conf.SAMTOOLS):
    """DEPRECATED. Kept for testing.

    Uses samtools depth to figure out Bonferroni factor
    automatically. Will ignore zero-coverage regions as opposed to
    auto_bonf_factor
    """

    assert os.path.exists(bam)
    if bed_file:
        assert os.path.exists(bed_file)

    samtools_depth = [samtools, 'depth', 
                      '-q', "%d" % min_base_q, 
                      '-Q', "%d" % min_map_q]
    if bed_file:
        samtools_depth.extend(['-b', bed_file])
    samtools_depth.append(bam)
    
    try:
        p1 = subprocess.Popen(samtools_depth, 
                              stdout=subprocess.PIPE)
        p2 = subprocess.Popen(["wc", "-l"], 
                              stdin=p1.stdout, stdout=subprocess.PIPE)
    except OSError:
        LOG.error("Can't execute either %s or wc" % samtools)
        raise
    
    p1.stdout.close()# allow p1 to receive a SIGPIPE if p2 exits.
    num_cov_cols = p2.communicate()[0]
   
    bonf_factor = int(num_cov_cols) * 3
    return bonf_factor


def auto_bonf_factor_from_depth(bam, bed_file=None,
                                min_base_q=3, min_map_q=0):
    """Determines how many columns have coverage in BAM to figure out
    Bonferroni factor automatically. Will ignore zero-coverage regions
    as opposed to auto_bonf_factor

    >>> FIXME doctest against __auto_bonf_factor_from_depth
    """

    assert os.path.exists(bam)
    if bed_file:
        assert os.path.exists(bed_file)


    (mean_depth, num_nonzero_cols) = depth_stats(
        bam, bed=bed_file, min_baseq=min_base_q, min_mapq=min_map_q)

    bonf_factor = int(num_nonzero_cols) * 3
    return bonf_factor


#if __name__ == "__main__":
#    import doctest
#    doctest.testmod()
    
