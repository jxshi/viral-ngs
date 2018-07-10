"""Unit tests for kmers.py"""

__author__ = "ilya@broadinstitute.org"

import os
import collections
import argparse

from test import assert_equal_contents, assert_equal_bam_reads, make_slow_test_marker

import Bio.SeqIO
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from Bio.Alphabet import IUPAC
import pytest

import kmers
import util.cmd
import util.file
import util.misc
import tools.kmc
import tools.samtools

slow_test = make_slow_test_marker()  # pylint: disable=invalid-name

def _inp(fname):
    '''Return the full filename for a file in the test input directory'''
    return os.path.join(util.file.get_test_input_path(), fname)

class TestKmers(object):

    """Test commands for manipulating kmers"""

    @staticmethod
    def _getargs(args, valid_args):
        """Extract valid args from a Namespace or a dict.  Returns a dict containing keys from `args`
        that are in `valid_args`; `valid_args` is a space-separated list of valid args."""
        return util.misc.subdict(vars(args) if isinstance(args, argparse.Namespace) else args, valid_args.split())

    @staticmethod
    def _revcomp(kmer):
        """Return the reverse complement of a kmer, given as a string"""
        return str(Seq(kmer, IUPAC.unambiguous_dna).reverse_complement())

    def _canonicalize(self, kmer):
        """Return the canonical version of a kmer"""
        return min(kmer, self._revcomp(kmer))

    @staticmethod
    def _seq_as_str(s):  # pylint: disable=invalid-name
        """Return a sequence as a str, regardless of whether it was a str, a Seq or a SeqRecord"""
        if isinstance(s, Seq):
            return str(s)
        if isinstance(s, SeqRecord):
            return str(s.seq)
        return s

    def _yield_seqs_as_strs(self, seqs):
        """Yield sequence(s) from `seqs` as strs.  seqs can be a str/SeqRecod/Seq, a filename of a sequence file,
        or an iterable of these.  If a filename of a sequence file, all sequences from that file are yielded."""
        for seq in util.misc.make_seq(seqs, (str, SeqRecord, Seq)):
            seq = self._seq_as_str(seq)
            if not any(seq.endswith(ext) for ext in '.fasta .fasta.gz .fastq .fastq.gz .bam'):
                yield seq
            else:
                with util.file.tmp_dir(suffix='seqs_as_strs') as t_dir:
                    if seq.endswith('.bam'):
                        t_fa = os.path.join(t_dir, 'bam2fa.fasta')
                        tools.samtools.SamtoolsTool().bam2fa(seq, t_fa)
                        seq = t_fa
                    with util.file.open_or_gzopen(seq, 'rt') as seq_f:
                        for rec in Bio.SeqIO.parse(seq_f, util.file.uncompressed_file_type(seq)[1:]):
                            yield str(rec.seq)
    #
    # To help generate the expected correct output, we reimplement in simple Python code some
    # of KMC's functionality.   This is also to make up for KMC's lack of a public test suite
    # ( https://github.com/refresh-bio/KMC/issues/55 ).
    #

    def _compute_kmers(self, seqs, kmer_size, single_strand):
        """Yield kmers of seq(s).  Unless `single_strand` is True, each kmer
        is canonicalized before being returned.  Note that
        deduplication is not done, so each occurrence of each kmer is
        yielded.  Kmers containing non-TCGA bases are skipped.
        """
        for seq in self._yield_seqs_as_strs(seqs):
            n_kmers = len(seq)-kmer_size+1

            # mark kmers containing invalid base(s)
            valid_kmer = [True] * n_kmers
            for i in range(len(seq)):  # pylint: disable=consider-using-enumerate
                if seq[i] not in 'TCGA':
                    invalid_lo = max(0, i-kmer_size+1)
                    invalid_hi = min(n_kmers, i+1)
                    valid_kmer[invalid_lo:invalid_hi] = [False]*(invalid_hi-invalid_lo)

            for i in range(n_kmers):
                if valid_kmer[i]:
                    kmer = seq[i:i+kmer_size]
                    yield kmer if single_strand else self._canonicalize(kmer)

    def _compute_kmer_counts(self, seqs, kmer_size, min_occs=None, max_occs=None,
                             counter_cap=None, single_strand=False, threads=None):
        """Yield kmer counts of seq(s).  Unless `single_strand` is True, each kmer is
        canonicalized before being counted.  Kmers containing non-TCGA bases are skipped.
        Kmers with fewer than `min_occs` or more than `max_occs` occurrences
        are dropped, and kmer counts capped at `counter_cap`, if these args are given.
        `threads` is currently ignored but needed so we can call this routine with same args
        as kmc.
        """
        counts = collections.Counter(self._compute_kmers(seqs, kmer_size, single_strand))
        if any((min_occs, max_occs, counter_cap)):
            counts = dict((kmer, min(count, counter_cap or count)) \
                          for kmer, count in counts.items() \
                          if (count >= (min_occs or count)) and \
                          (count <= (max_occs or count)))
        return counts

    @staticmethod
    def _make_SeqRecords(seqs):  # pylint: disable=invalid-name
        """Given seq(s) as str(s), return a list of SeqRecords with these seq(s)"""
        return [SeqRecord(Seq(seq, IUPAC.unambiguous_dna),
                          id='seq_{}'.format(i), name='seq_{}'.format(i),
                          description='sequence number {}'.format(i))
                for i, seq in enumerate(util.misc.make_seq(seqs))]

    def _write_seqs_to_fasta(self, seqs, seqs_fasta):
        """Write a .fasta file with the given seq(s)."""
        Bio.SeqIO.write(self._make_SeqRecords(seqs), seqs_fasta, 'fasta')

    def _build_and_check_kmer_db(self, seq_files, kmer_db, **kwargs):
        """Build a database of kmers from given sequence file(s), and check its correctness.  Args are the same as to
        kmers.buidl_kemr_db()."""
        kmers.build_kmer_db(seq_files, kmer_db, mem_limit_gb=4, **kwargs)
        kmers_txt = kmer_db+'.kmer_counts.txt'
        kmers.dump_kmer_counts(kmer_db, kmers_txt)
        assert tools.kmc.KmcTool().read_kmer_counts(kmers_txt) == self._compute_kmer_counts(seq_files, **kwargs)
        return kmer_db

    @slow_test
    @pytest.mark.parametrize("seq_files", [['ebola.fasta'], ['empty.bam']])
    @pytest.mark.parametrize("kmer_size", [11, 31])
    @pytest.mark.parametrize("counter_cap", [255, 32])
    @pytest.mark.parametrize("single_strand", [False, True])
    def test_build_kmer_db_1(self, seq_files, kmer_size, counter_cap, single_strand):
        self._test_build_kmer_db(seq_files=seq_files, kmer_size=kmer_size, counter_cap=counter_cap, single_strand=single_strand)

    @pytest.mark.parametrize("seq_files", [['ebola.fasta.gz'], ['empty.bam'], ['empty.bam', 'almost-empty-2.bam']])
    @pytest.mark.parametrize("kmer_size", [17])
    def test_build_kmer_db_2(self, seq_files, kmer_size):
        self._test_build_kmer_db(seq_files=seq_files, kmer_size=kmer_size)

    @pytest.mark.parametrize("seq_files", [['test-reads.bam',
                                            'test-reads-human.bam']])
    @pytest.mark.parametrize("kmer_size", [17])
    def test_build_kmer_db_two_files(self, seq_files, kmer_size):
        self._test_build_kmer_db(seq_files=seq_files, kmer_size=kmer_size)

    @pytest.mark.parametrize("seq_files", [['empty.fasta']])
    @pytest.mark.parametrize("kmer_size", [1])
    @pytest.mark.parametrize("threads", [10, pytest.param(11, marks=pytest.mark.xfail)])
    def test_build_kmer_db_fail_on_over_10_threads(self, seq_files, kmer_size, threads):
        self._test_build_kmer_db(seq_files=seq_files, kmer_size=kmer_size, threads=threads)


    def _test_build_kmer_db(self, seq_files, **kwargs):
        with util.file.tmp_dir(suffix='test_build_kmer_db') as t_dir:
            self._build_and_check_kmer_db(seq_files=[os.path.join(util.file.get_test_input_path(self), f)
                                                     for f in util.misc.make_seq(seq_files)],
                                          kmer_db=os.path.join(t_dir, 'kmer_db'),
                                          **kwargs)

    @pytest.mark.parametrize("seqs,opts", [
        ('A'*15, '-k 4'),
        ('T'*15, '-k 4'),
        ([], '-k 1 --threads 1'),
        (['TCGA'*3, 'ATTT'*5], '-k 7 --threads 1'),
        pytest.param(['TCGA'*3, 'ATTT'*5], '-k 7 --threads 2'),
        (['TCGA'*3, 'ATTT'*5], '-k 31'),
    ])
    def test_kmer_extraction(self, seqs, opts):
        with util.file.tmp_dir(suffix='kmctest') as t_dir:
            seqs_fasta = os.path.join(t_dir, 'seqs.fasta')
            self._write_seqs_to_fasta(seqs, seqs_fasta)
            kmer_db = os.path.join(t_dir, 'kmer_db')
            args = util.cmd.run_cmd(kmers, 'build_kmer_db',
                                    opts.split() + ['--memLimitGb', 4] + [seqs_fasta, kmer_db]).args_parsed

            kmers_txt = os.path.join(t_dir, 'kmers.txt')
            util.cmd.run_cmd(kmers, 'dump_kmer_counts', [kmer_db, kmers_txt])
            assert tools.kmc.KmcTool().read_kmer_counts(kmers_txt) == \
                self._compute_kmer_counts(seqs,
                                          **self._getargs(args,
                                                          'kmer_size single_strand min_occs max_occs counter_cap'))

    # to test:
    #   empty bam, empty fasta, empty both
    #   getting kmers from bam (here just convert the fasta to it?), from fastq,
    #   from multiple files.
    #   ambiguity codes, gaps, Ns
    #   bams with different mixes of read groups, mix of paired/unpaired etc

    def _filter_seqs(self, db_kmer_counts, seqs, kmer_size, single_strand, read_min_occs=None, read_max_occs=None):
        seqs_out = []
        read_min_occs, read_max_occs = tools.kmc.KmcTool()._infer_filter_reads_params(read_min_occs, read_max_occs) # pylint: disable=protected-access

        for seq in util.misc.make_seq(seqs, (str, SeqRecord, Seq)):
            seq_kmer_counts = self._compute_kmer_counts(seq, kmer_size=kmer_size, single_strand=single_strand)
            seq_occs = sum([seq_count for kmer, seq_count in seq_kmer_counts.items() \
                            if kmer in db_kmer_counts])
            read_min_occs_seq, read_max_occs_seq = map(lambda v: int(v*(len(seq)-kmer_size+1)) # pylint: disable=cell-var-from-loop
                                                       if isinstance(v, float) else v,
                                                       (read_min_occs, read_max_occs))
            #print('seq=', seq, 'kmer_counts=', seq_kmer_counts, 'seq_occs=', seq_occs)

            if (read_min_occs_seq or seq_occs) <= seq_occs <= (read_max_occs_seq or seq_occs):
                seqs_out.append(seq)

        return seqs_out

    @pytest.mark.parametrize("kmer_size", [17, 31])
    @pytest.mark.parametrize("single_strand", [False, True])
    @pytest.mark.parametrize("read_min_occs", [80, .7])
    @pytest.mark.parametrize("kmers_fasta,reads_bam", [
        ('ebola.fasta', 'G5012.3.subset.bam'),
    ])
    def test_read_filtering(self, kmer_size, single_strand, kmers_fasta, reads_bam, read_min_occs):
        self._test_read_filtering(kmer_size, single_strand, kmers_fasta, reads_bam, read_min_occs)

    @pytest.mark.xfail(reason="kmc bug, reported at https://github.com/refresh-bio/KMC/issues/86")
    @pytest.mark.parametrize("args", [dict(kmer_size=1, single_strand=False, read_min_occs=1, kmers_fasta='empty.fasta',
                                           reads_bam='empty.bam')])
    def test_read_filtering_fail_1(self, args):
        self._test_read_filtering(**args)

    def _test_read_filtering(self, kmer_size, single_strand, kmers_fasta, reads_bam,
                             read_min_occs=None, read_max_occs=None):
        """Test read filtering.

        Args:
          kmer_size: kmer size
          single_strand: whether to canonicalize kmers
          kmers_fasta: kmers for filtering will be extracted from here
          reads_bam: reads to filter with kmers extracted from kmers_fasta

        """
        with util.file.tmp_dir(suffix='kmctest_reafilt') as t_dir:
            def _cmdflag(flag, val):
                return [] if val in (None, False) else ([flag] if val is True else [flag, val])

            kmers_fasta = os.path.join(util.file.get_test_input_path(), kmers_fasta)
            kmer_db = os.path.join(t_dir, 'kmer_db')
            kmer_db_args = util.cmd.run_cmd(kmers, 'build_kmer_db',
                                            ['--memLimitGb', 4, '-k', kmer_size, kmers_fasta, kmer_db]
                                            +_cmdflag('--singleStrand', single_strand)).args_parsed

            kmers_fasta_seqs = list(Bio.SeqIO.parse(kmers_fasta, 'fasta'))
            kmers_fasta_kmer_counts = self._compute_kmer_counts(kmers_fasta_seqs,
                                                                **self._getargs(kmer_db_args,
                                                                                'kmer_size single_strand '
                                                                                'min_occs max_occs counter_cap'))

            kmers_txt = os.path.join(t_dir, 'kmers.txt')
            util.cmd.run_cmd(kmers, 'dump_kmer_counts', [kmer_db, kmers_txt])

            kmc_kmer_counts = tools.kmc.KmcTool().read_kmer_counts(kmers_txt)

            assert len(kmc_kmer_counts) == len(kmers_fasta_kmer_counts)
            assert kmc_kmer_counts == kmers_fasta_kmer_counts

            reads_bam = os.path.join(util.file.get_test_input_path(), reads_bam)
            with tools.samtools.SamtoolsTool().bam2fa_tmp(reads_bam) as (reads_1, reads_2):
                reads_1_filt = os.path.join(t_dir, 'ebola.reads.1.filt.fasta')
                util.cmd.run_cmd(kmers, 'filter_by_kmers', [kmer_db, reads_1, reads_1_filt] \
                                                            + _cmdflag('--readMinOccs', read_min_occs) \
                                                            + _cmdflag('--readMaxOccs', read_max_occs))
                reads_1_seqs = tuple(Bio.SeqIO.parse(reads_1, 'fasta'))

                reads_1_filt_expected = self._filter_seqs(kmc_kmer_counts, reads_1_seqs,
                                                          kmer_size=kmer_size, single_strand=single_strand,
                                                          read_min_occs=read_min_occs)
                reads_1_filt_seqs = tuple(Bio.SeqIO.parse(reads_1_filt, 'fasta'))
                #assert 0 < len(reads_1_filt_seqs) < len(reads_1_seqs)
                def SeqRecord_data(r):  # pylint: disable=invalid-name
                    return (r.id, r.seq)
                assert  sorted(map(SeqRecord_data, reads_1_filt_seqs)) == \
                    sorted(map(SeqRecord_data, reads_1_filt_expected))

    # end: def _test_read_filtering(self, kmer_size, single_strand, kmers_fasta, reads_bam,
    #                               read_min_occs=None, read_max_occs=None)

# end: class TestKmers(object)
