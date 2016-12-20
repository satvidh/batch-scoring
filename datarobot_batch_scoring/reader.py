import codecs
import csv
import gzip
import io
import multiprocessing
import os
import signal
import sys
from itertools import chain
from time import time
import chardet
import six

from datarobot_batch_scoring.consts import (Batch,
                                            ProgressQueueMsg)
from datarobot_batch_scoring.utils import get_rusage

if six.PY2:
    import StringIO


DETECT_SAMPLE_SIZE_FAST = int(0.2 * 1024 ** 2)
DETECT_SAMPLE_SIZE_SLOW = 1024 ** 2
AUTO_SAMPLE_SIZE = int(0.5 * 1024 ** 2)
AUTO_SMALL_SAMPLES = 500
AUTO_GOAL_SIZE = int(2.5 * 1024 ** 2)  # size we want per batch


def decode_reader_state(ch):
    return {
        b"-": "Initial",
        b"P": "Posting to queue",
        b"A": "Aborted",
        b"D": "Done",
        b"C": "CSV Error",
        b"E": "Error"
    }.get(ch)


def fast_to_csv_chunk(data, header):
    """Fast routine to format data for prediction api.

    Returns data in unicode.
    """
    header = ','.join(header)
    chunk = ''.join(chain((header, os.linesep), data))
    if six.PY3:
        return chunk.encode('utf-8')
    else:
        return chunk


def slow_to_csv_chunk(data, header):
    """Slow routine to format data for prediction api.
    Returns data in unicode.
    """
    if six.PY3:
        buf = io.StringIO()
    else:
        buf = io.BytesIO()

    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(data)
    if six.PY3:
        return buf.getvalue().encode('utf-8')
    else:
        return buf.getvalue()


class Recoder:
    """
    Iterator that reads an encoded stream and decodes the input to UTF-8
    for Python 2. In Python 3 the open function decodes the file.
    """
    def __init__(self, f, encoding):
        f.seek(0)
        if six.PY3:
            self.reader = f
        if six.PY2:
            self.reader = codecs.StreamRecoder(f,
                                               codecs.getencoder('utf-8'),
                                               codecs.getdecoder('utf-8'),
                                               codecs.getreader(encoding),
                                               codecs.getwriter(encoding))

    def __iter__(self):
        return self

    def next(self):   # python 3
        return self.reader.next()

    def __next__(self):  # python 2
        return self.reader.__next__()


class CSVReader(object):
    def __init__(self, fd, encoding, ui):
        self.fd = fd
        #  dataset_dialect is set by investigate_encoding_and_dialect in utils
        self.dialect = csv.get_dialect('dataset_dialect')
        self.encoding = encoding
        self._ui = ui

    def _create_reader(self):
        fd = Recoder(self.fd, self.encoding)
        return csv.reader(fd, self.dialect, delimiter=self.dialect.delimiter)


class FastReader(CSVReader):
    """A reader that only reads the file in text mode but not parses it. """

    def __init__(self, fd, encoding, ui):
        super(FastReader, self).__init__(fd, encoding, ui)
        self._check_for_multiline_input()
        reader = self._create_reader()
        self.header = next(reader)
        self.fieldnames = [c.strip() for c in self.header]

    def __iter__(self):
        fd = Recoder(self.fd, self.encoding)
        it = iter(fd)
        next(it)  # skip header
        return it

    def _check_for_multiline_input(self, peek_size=100):
        # peek the first `peek_size` records for multiline CSV
        reader = self._create_reader()
        i = 0
        for line in reader:
            i += 1
            if i == peek_size:
                break

        peek_size = min(i, peek_size)

        if reader.line_num != peek_size:
            self._ui.fatal('Detected multiline CSV format'
                           ' -- dont use flag `--fast` '
                           'to force CSV parsing. '
                           'Note that this will slow down scoring.')


class SlowReader(CSVReader):
    """The slow reader does actual CSV parsing.
    It supports multiline csv and can be a factor of 50 slower. """

    def __init__(self, fd, encoding, ui):
        super(SlowReader, self).__init__(fd, encoding, ui)
        reader = self._create_reader()
        self.header = next(reader)
        self.fieldnames = [c.strip() for c in self.header]

    def __iter__(self):
        self.reader = self._create_reader()
        for i, row in enumerate(self.reader):
            if i == 0:
                # skip header
                continue
            yield row


def iter_chunks(csvfile, chunk_size):
    chunk = []
    for row in csvfile:
        chunk.append(row)
        if len(chunk) >= chunk_size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


class BatchGenerator(object):
    """Class to chunk a large csv files into a stream
    of batches of size ``--n_samples``.

    Yields
    ------
    batch : Batch
        The next batch. A batch holds the data to be send already
        in the form that can be passed to the HTTP request.
    """

    def __init__(self, dataset, n_samples, n_retry, delimiter, ui,
                 fast_mode, encoding, already_processed_batches=set()):
        self.dataset = dataset
        self.chunksize = n_samples
        self.rty_cnt = n_retry
        self._ui = ui
        self.fast_mode = fast_mode
        self.encoding = encoding
        self.already_processed_batches = already_processed_batches

    def csv_input_file_reader(self):
        if self.dataset.endswith('.gz'):
            opener = gzip.open
        else:
            opener = open

        if six.PY3:
            fd = opener(self.dataset, 'rt',
                        encoding=self.encoding)
        else:
            fd = opener(self.dataset, 'rb')
        return fd

    def __iter__(self):
        if self.fast_mode:
            reader_factory = FastReader
        else:
            reader_factory = SlowReader

        with self.csv_input_file_reader() as csvfile:
            reader = reader_factory(csvfile, self.encoding, self._ui)
            fieldnames = reader.fieldnames

            has_content = False
            t0 = time()
            rows_read = 0
            for chunk in iter_chunks(reader, self.chunksize):
                has_content = True
                n_rows = len(chunk)
                if (rows_read, n_rows) not in self.already_processed_batches:
                    yield Batch(rows_read, n_rows, fieldnames,
                                chunk, self.rty_cnt)
                rows_read += n_rows
            if not has_content:
                raise ValueError("Input file '{}' is empty.".format(
                    self.dataset))
            self._ui.info('chunking {} rows took {}'.format(rows_read,
                                                            time() - t0))


class Shovel(object):

    def __init__(self, queue, progress_queue, shovel_status,
                 abort_flag, batch_gen_args, ui):
        self._ui = ui
        self.queue = queue
        self.progress_queue = progress_queue
        self.shovel_status = shovel_status
        self.abort_flag = abort_flag
        self.batch_gen_args = batch_gen_args
        self.dialect = csv.get_dialect('dataset_dialect')
        #  The following should only impact Windows
        self._ui.set_next_UI_name('batcher')

    def exit_fast(self, a, b):
        self._ui.debug("exit_fast: {} {}".format(a, b))
        self.shovel_status.value = b"A"
        os._exit(1)

    def _shove(self, args, dialect, queue):
        signal.signal(signal.SIGINT, self.exit_fast)
        signal.signal(signal.SIGTERM, self.exit_fast)
        t2 = time()
        _ui = args[4]
        _ui.info('Shovel process started')
        csv.register_dialect('dataset_dialect', dialect)
        batch_generator = BatchGenerator(*args)
        try:
            n = 0
            for batch in batch_generator:
                _ui.debug('queueing batch {}'.format(batch.id))
                self.shovel_status.value = b"P"
                queue.put(batch)
                n += 1
                if self.abort_flag.value:
                    _ui.info('shoveling abort requested')
                    self.shovel_status.value = b"A"
                    break

            self.shovel_status.value = b"D"
            _ui.info('shoveling complete | total time elapsed {}s'
                     ''.format(time() - t2))
            self.progress_queue.put((ProgressQueueMsg.SHOVEL_DONE,
                                     {
                                         "produced": n,
                                         "rusage": get_rusage()
                                     }))
        except csv.Error as e:
            self.shovel_status.value = b"C"
            self.progress_queue.put((ProgressQueueMsg.SHOVEL_CSV_ERROR,
                                     {
                                         "batch": batch._replace("data", []),
                                         "error": str(e),
                                         "produced": n,
                                         "rusage": get_rusage()
                                     }))
            raise
        except Exception as e:
            self.shovel_status.value = b"E"
            self.progress_queue.put((ProgressQueueMsg.SHOVEL_ERROR,
                                     {
                                         "batch": batch._replace("data", []),
                                         "error": str(e),
                                         "produced": n,
                                         "rusage": get_rusage()
                                     }))
            raise
        finally:
            if os.name is 'nt':
                _ui.close()

    def go(self):
        self.p = multiprocessing.Process(target=self._shove,
                                         args=([self.batch_gen_args,
                                                self.dialect, self.queue]),
                                         name='Shovel_Proc')
        self.p.start()
        return self.p

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.p.is_alive():
            self.p.terminate()


def investigate_encoding_and_dialect(dataset, sep, ui, fast=False,
                                     encoding=None, skip_dialect=False,
                                     output_delimiter=None):
    """Try to identify encoding and dialect.
    Providing a delimiter may help with smaller datasets.
    Running this is costly so run it once per dataset."""
    t0 = time()
    if fast:
        sample_size = DETECT_SAMPLE_SIZE_FAST
    else:
        sample_size = DETECT_SAMPLE_SIZE_SLOW

    if dataset.endswith('.gz'):
        opener = gzip.open
    else:
        opener = open
    with opener(dataset, 'rb') as dfile:
        sample = dfile.read(sample_size)

    if not encoding:
        chardet_result = chardet.detect(sample)
        ui.debug('investigate_encoding_and_dialect - seconds to detect '
                 'encoding: {}'.format(time() - t0))
        encoding = chardet_result['encoding'].lower()
    else:
        ui.debug('investigate_encoding_and_dialect - skip encoding detect')
        encoding = encoding.lower()
        sample[:1000].decode(encoding)  # Fail here if the encoding is invalid
    t1 = time()
    try:
        if skip_dialect:
            ui.debug('investigate_encoding_and_dialect - skip dialect detect')
            if sep:
                csv.register_dialect('dataset_dialect', csv.excel,
                                     delimiter=sep)
            else:
                csv.register_dialect('dataset_dialect', csv.excel)
            dialect = csv.get_dialect('dataset_dialect')
        else:
            sniffer = csv.Sniffer()
            dialect = sniffer.sniff(sample.decode(encoding), delimiters=sep)
            ui.debug('investigate_encoding_and_dialect - seconds to detect '
                     'csv dialect: {}'.format(time() - t1))
    except csv.Error:
        if len(sample) < 10:
            ui.fatal('Input file "%s" is less than 10 chars long '
                     'and this is the possible cause of a csv.Error.'
                     ' Check the file and try again.' % dataset)
        elif sep is not None:
            ui.fatal('The csv module failed to detect the CSV '
                     'dialect. Check that you provided the correct '
                     'delimiter, or try the script without the '
                     '--delimiter flag.')
        else:
            ui.fatal('The csv module failed to detect the CSV '
                     'dialect. Try giving hints with the '
                     '--delimiter argument, E.g  '
                     """--delimiter=','""")
        raise
    #  in Python 2, csv.dialect sometimes returns unicode which the
    #  PY2 csv.reader cannot handle. This may be from the Recoder
    if six.PY2:
        for a in ['delimiter', 'lineterminator', 'quotechar']:
            if isinstance(getattr(dialect, a, None), type(u'')):
                recast = str(getattr(dialect, a))
                setattr(dialect, a, recast)
    csv.register_dialect('dataset_dialect', dialect)
    #  the csv writer should use the systems newline char
    csv.register_dialect('writer_dialect', dialect,
                         lineterminator=os.linesep,
                         delimiter=str(output_delimiter or dialect.delimiter))
    ui.debug('investigate_encoding_and_dialect - total time seconds -'
             ' {}'.format(time() - t0))
    ui.debug('investigate_encoding_and_dialect - encoding -'
             ' {}'.format(encoding))
    values = ['delimiter', 'doublequote', 'escapechar', 'lineterminator',
              'quotechar', 'quoting', 'skipinitialspace', 'strict']
    d_attr = ' '.join(['{}={} '.format(i, repr(getattr(dialect, i))) for i in
                      values if hasattr(dialect, i)])
    ui.debug('investigate_encoding_and_dialect - vars(dialect) - {}'
             ''.format(d_attr))
    return encoding


def auto_sampler(dataset, encoding, ui):
    """
    Automatically find an appropriate number of rows to send per batch based
    on the average row size.
    :return:
    """

    t0 = time()

    sample_size = AUTO_SAMPLE_SIZE
    if dataset.endswith('.gz'):
        opener = gzip.open
    else:
        opener = open
    with opener(dataset, 'rb') as dfile:
        sample = dfile.read(sample_size)
    ingestable_sample = sample.decode(encoding)
    size_bytes = sys.getsizeof(ingestable_sample.encode('utf-8'))

    if size_bytes < (sample_size * 0.75):
        #  if dataset is tiny, don't bother auto sampling.
        ui.info('auto_sampler: total time seconds - {}'.format(time() - t0))
        ui.info('auto_sampler: defaulting to {} samples for small dataset'
                .format(AUTO_SMALL_SAMPLES))
        return AUTO_SMALL_SAMPLES

    if six.PY3:
        buf = io.StringIO()
        buf.write(ingestable_sample)
    else:
        buf = StringIO.StringIO()
        buf.write(sample)
    buf.seek(0)
    file_lines, csv_lines = 0, 0
    dialect = csv.get_dialect('dataset_dialect')
    fd = Recoder(buf, encoding)
    reader = csv.reader(fd, dialect=dialect, delimiter=dialect.delimiter)
    line_pos = []
    for _ in buf:
        file_lines += 1
        line_pos.append(buf.tell())
    #  remove the last line since it's probably not fully formed
    buf.truncate(line_pos[-2])
    buf.seek(0)
    file_lines -= 1
    try:
        for _ in reader:
            csv_lines += 1
    except csv.Error:
        if buf.tell() in line_pos[-3:]:
            ui.debug('auto_sampler: caught csv.Error at end of sample. '
                     'seek_position: {}, csv_line: {}'.format(buf.tell(),
                                                              line_pos))
        else:
            ui.fatal('--auto_sample failed to parse the csv file. Try again '
                     'without --auto_sample. seek_position: {}, '
                     'csv_line: {}'.format(buf.tell(), line_pos))
            raise
    else:
        ui.debug('auto_sampler: analyzed {} csv rows'.format(csv_lines))

    buf.close()
    avg_line = int(size_bytes / csv_lines)
    chunk_size_goal = AUTO_GOAL_SIZE  # size we want per batch
    lines_per_sample = int(chunk_size_goal / avg_line) + 1
    ui.debug('auto_sampler: lines counted: {},  avgerage line size: {}, '
             'recommended lines per sample: {}'.format(csv_lines, avg_line,
                                                       lines_per_sample))
    ui.info('auto_sampler: total time seconds - {}'.format(time() - t0))
    return lines_per_sample


def peek_row(dataset, delimiter, ui, fast_mode, encoding):
    """Peeks at the first row in `dataset`. """
    batches = BatchGenerator(dataset, 1, 1, delimiter, ui, fast_mode,
                             encoding)
    try:
        batch = next(iter(batches))
    except StopIteration:
        raise ValueError('Cannot peek first row from {}'.format(dataset))
    return batch
