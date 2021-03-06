from queue import Queue
from threading import Thread

from ipykernel.kernelbase import Kernel
import subprocess
import logging
import tempfile
import os

class RealTimeSubprocess(subprocess.Popen):
    """
    A subprocess that allows to read its stdout and stderr in real time
    """

    def __init__(self, cmd, write_to_stdout, write_to_stderr):
        """
        :param cmd: the command to execute
        :param write_to_stdout: a callable that will be called with chunks of data from stdout
        :param write_to_stderr: a callable that will be called with chunks of data from stderr
        """
        self._write_to_stdout = write_to_stdout
        self._write_to_stderr = write_to_stderr

        super().__init__(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

        self._stdout_queue = Queue()
        self._stdout_thread = Thread(target=RealTimeSubprocess._enqueue_output, args=(self.stdout, self._stdout_queue))
        self._stdout_thread.daemon = True
        self._stdout_thread.start()

        self._stderr_queue = Queue()
        self._stderr_thread = Thread(target=RealTimeSubprocess._enqueue_output, args=(self.stderr, self._stderr_queue))
        self._stderr_thread.daemon = True
        self._stderr_thread.start()

    @staticmethod
    def _enqueue_output(stream, queue):
        """
        Add chunks of data from a stream to a queue until the stream is empty.
        """
        for line in iter(lambda: stream.read(4096), b''):
            queue.put(line)
        stream.close()

    def write_contents(self):
        """
        Write the available content from stdin and stderr where specified when the instance was created
        :return:
        """

        def read_all_from_queue(queue):
            res = b''
            size = queue.qsize()
            while size != 0:
                res += queue.get_nowait()
                size -= 1
            return res

        stdout_contents = read_all_from_queue(self._stdout_queue)
        if stdout_contents:
            self._write_to_stdout(stdout_contents)
        stderr_contents = read_all_from_queue(self._stderr_queue)
        if stderr_contents:
            self._write_to_stderr(stderr_contents)


class CppKernel(Kernel):
    implementation = 'jupyter_cpp_kernel'
    implementation_version = '0.0.2'
    language = 'c++'
    language_version = 'c++11'
    language_info = {
        'name': 'c++',
        'codemirror_mode': 'c++',
        'mimetype': 'text/x-c++src',
        'file_extension': '.cc',
    }
    banner = "cppkernel"

    def __init__(self, *args, **kwargs):
        self.files = []
        self.file_suffix = '.cc'
        self.compiler = 'g++'
        super(CppKernel, self).__init__(*args, **kwargs)

    def cleanup_files(self):
        "Remove all the temporary files created by the kernel"
        for file in self.files:
            os.remove(file)

    def new_temp_file(self, **kwargs):
        """Create a new temp file to be deleted when the kernel shuts down"""
        kwargs['delete'] = False
        kwargs['mode'] = 'w'
        file = tempfile.NamedTemporaryFile(**kwargs)
        self.files.append(file.name)
        return file

    def _write_to_stdout(self, contents):
        self.send_response(self.iopub_socket, 'stream', {'name': 'stdout', 'text': contents})

    def _write_to_stderr(self, contents):
        self.send_response(self.iopub_socket, 'stream', {'name': 'stderr', 'text': contents})

    def create_jupyter_subprocess(self, cmd):
        self._write_to_stderr('running cmd: %s\n' % ' '.join(cmd))
        return RealTimeSubprocess(cmd,
                                  lambda contents: self._write_to_stdout(contents.decode()),
                                  lambda contents: self._write_to_stderr(contents.decode()))

    def _magic(self, code):
        topcodes = []
        add_main = True
        cflags = []
        ldflags = []
        for line in code.splitlines():
            line = line.strip()
            if line.startswith('//%'):
                key, value = line[3:].strip().split(':')
                key = key.strip()
                value = value.strip()
                headers = "iostream vector string set map functional".split()
                logging.warning("%s:%s" % (key, value))
                print ("%s:%s" % (key, value))
                # set compiler
                if key == 'compiler':
                    self.compiler = value
                    if "nvcc" in value:
                        self.file_suffix = '.cu'
                if key == 'suffix':
                    self.file_suffix = value
                if key == 'includes':
                    if value != 'full':
                        headers = value.split()
                    for header in headers:
                        topcodes.append('#include <%s>' % header)
                if key == 'namespace':
                    topcodes.append('using namespace %s;' % value)
                if key == 'main' and value == 'no':
                    add_main = False
                if key == 'cflags':
                    for v in value.strip().split():
                        cflags.append(v)
                if key == 'ldflags':
                    for v in value.strip().split():
                        ldflags.append(v)

        if add_main:
            code = '\n'.join(topcodes) + '\nint main() {\n' + code + '\nreturn 0;\n}'
        else:
            code = '\n'.join(topcodes) + code

        return code, cflags, ldflags


    def compile(self, compiler, source_filename, binary_filename, cflags=None, ldflags=None):
        cflags = ['-std=c++11'] + cflags
        args = [compiler, source_filename] + cflags + ['-o', binary_filename] + ldflags
        return self.create_jupyter_subprocess(args)

    def do_execute(self, code, silent, store_history=True,
                   user_expressions=None, allow_stdin=False):

        code, cflags, ldflags = self._magic(code)
        with self.new_temp_file(suffix=self.file_suffix) as source_file:
            source_file.write(code)
            source_file.flush()
            with self.new_temp_file(suffix='.out') as binary_file:
                p = self.compile(self.compiler, source_file.name, binary_file.name, cflags, ldflags)
                while p.poll() is None:
                    p.write_contents()
                p.write_contents()
                if p.returncode != 0: # compile failed
                    self._write_to_stderr(
                        "[c++ kernel] g++ execute with code {}, the executable will not be executed".format(
                            p.returncode)
                    )
                    return {'status': 'ok', 'execution_count': self.execution_count, 'payload':[],
                            'user_expression': {}}

        # execute it
        p = self.create_jupyter_subprocess([binary_file.name])
        while p.poll() is None:
            p.write_contents()
        p.write_contents()

        if p.returncode != 0:
            self._write_to_stderr("[c++ kernel] Executable exited with code {}".format(p.returncode))
        return {'status': 'ok', 'execution_count': self.execution_count, 'payload': [], 'user_expression':{}}

    def do_shutdown(self, restart):
        """Cleanup the created source code files and executables when shutting down the kernel"""
        self.cleanup_files()
