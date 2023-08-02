#!/usr/bin/env python3

"""Run a test for each testfile passed as an argument."""

# Imported packages
from collections import defaultdict
from difflib import context_diff
from functools import cache, cached_property
from itertools import chain
import logging
from logging import getLogger
from multiprocessing import Pool
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple, TypeVar
from shutil import copyfile, rmtree
from subprocess import CompletedProcess, run
import sys
from tempfile import mkdtemp

import click
from more_itertools import chunked, zip_equal
from tqdm import tqdm


# Global variables
LOGGER = getLogger(__name__)


# Configuration
LOG_LEVEL: int = logging.INFO

UPDATE_TESTS: bool = False
FAIL_FAST: bool = False

NUM_PROCESSES: Optional[int] = None  # None means that all available hyperthreads will be used.
TESTFILE_BATCH_SIZE: int = 10

MAX_TESTFILE_NAMES_SHOWN: int = 5
MAX_TESTFILE_NAMES_SHOWN_COLLAPSED: int = 3

TEMPLATE_DIRECTORY: Path = Path('templates')
SUPPORT_DIRECTORY: Path = Path('support')

COMMANDS_FILENAME = 'COMMANDS.m4'
TEMPLATE_HEAD_FILENAME: str = 'head.tex'
TEMPLATE_BODY_FILENAME: str = 'body.tex.m4'
TEMPLATE_FOOT_FILENAME: str = 'foot.tex'
TEST_FILENAME: str = 'test.tex'
TEST_OUTPUT_FILENAME: str = 'test.log'
TEST_ACTUAL_OUTPUT_FILENAME: str = 'test-actual.log'

TEST_SETUP_FILENAME_FORMAT: str = 'test-setup-{:03d}.tex'
TEST_INPUT_FILENAME_FORMAT: str = 'test-input-{:03d}.md'
TEST_EXPECTED_OUTPUT_FILENAME_FORMAT: str = 'test-expected-{:03d}.log'
TEST_ACTUAL_OUTPUT_FILENAME_FORMAT: str = 'test-actual-{:03d}.log'


# Types
T = TypeVar('T')

TestFile = Path
TestFileBatch = List[TestFile]

TeXFormat = str
Template = Path
Command = Tuple[str, ...]

SetupText = str
InputText = str
OutputText = str
OutputTextDiff = str


class ReadTestFile(NamedTuple):
    setup_text: SetupText
    input_text: InputText
    expected_output_text: OutputText


ReadTestFiles = List[ReadTestFile]


class TestParameters(NamedTuple):
    read_test_files: ReadTestFiles
    tex_format: TeXFormat
    template: Template
    command: Command


# TestSubResult type is defined in the implementation below.
# TestResult type is defined in the implementation below.
# BatchResult type is defined in the implementation below.


# Implementation
class TestSubResult:
    def __init__(self, batch_result: 'BatchResult', testfile_number: int):
        self.batch_result = batch_result
        self.testfile_number = testfile_number

    @property
    def testfile(self) -> TestFile:
        testfile = self.batch_result.testfile_batch[self.testfile_number]
        return testfile

    @property
    def test_parameters(self) -> TestParameters:
        test_parameters = self.batch_result.test_parameters
        return test_parameters

    @property
    def temporary_directory(self) -> Path:
        temporary_directory = self.batch_result.temporary_directory
        return temporary_directory

    @property
    def exit_code(self) -> int:
        exit_code = self.batch_result.exit_code
        return exit_code

    @property
    def read_test_file(self) -> ReadTestFile:
        read_test_file_result = self.test_parameters.read_test_files[self.testfile_number]
        return read_test_file_result

    @property
    def expected_output_text(self) -> OutputText:
        expected_output_text = self.read_test_file.expected_output_text
        return expected_output_text

    @property
    def actual_output_text(self) -> OutputText:
        try:
            actual_output_text = self.batch_result.actual_output_texts[self.testfile_number]
            return actual_output_text
        except KeyError:
            return ''  # We have already deleted temporary directory, or no output was produced due to an error.

    @cached_property
    def output_diff(self) -> OutputTextDiff:
        expected_output_lines = self.expected_output_text.splitlines()
        actual_output_lines = self.actual_output_text.splitlines()

        expected_output_filename = TEST_EXPECTED_OUTPUT_FILENAME_FORMAT.format(self.testfile_number)
        actual_output_filename = TEST_ACTUAL_OUTPUT_FILENAME_FORMAT.format(self.testfile_number)

        expected_output_file = self.temporary_directory / expected_output_filename
        actual_output_file = self.temporary_directory / actual_output_filename

        output_diff_lines = context_diff(
            expected_output_lines, actual_output_lines, fromfile=str(expected_output_file), tofile=str(actual_output_file), lineterm='')
        output_diff_text = '\n'.join(output_diff_lines)
        return output_diff_text

    @property
    def exited_successfully(self) -> bool:
        return self.exit_code == 0

    @property
    def output_matches(self) -> bool:
        if self.temporary_directory is None:
            return True  # We have already deleted temporary directory, output must have been the same.
        return not self.output_diff

    def __bool__(self) -> bool:
        return self.exited_successfully and self.output_matches


class TestResult:
    updated_testfile: Optional[bool] = None  # None means we haven't tried, False means we tried and failed, True means we succeeded.

    def __init__(self, subresults: Iterable[TestSubResult]) -> None:
        self.subresults = list(subresults)
        assert self.subresults  # Ensure that at least one subresult exists.

    @property
    def first_subresult(self) -> TestSubResult:
        first_subresult, *_ = self.subresults
        return first_subresult

    @property
    def testfile(self) -> TestFile:
        return self.first_subresult.testfile

    @property
    def setup_text(self) -> SetupText:
        return self.first_subresult.read_test_file.setup_text

    @property
    def input_text(self) -> InputText:
        return self.first_subresult.read_test_file.input_text

    @property
    def subresults_succeeded(self) -> bool:
        return all(self.subresults)

    @property
    def subresults_exited_successfully(self) -> bool:
        return all(subresult.exited_successfully for subresult in self.subresults)

    @property
    def subresult_outputs_match(self) -> bool:
        return all(subresult.output_matches for subresult in self.subresults)

    def try_to_update_testfile(self) -> None:
        assert self.updated_testfile is None  # Make sure that we don't run this method twice
        assert not self.subresults_succeeded  # Make sure that an update is needed

        if not self.subresults_exited_successfully:
            self.updated_testfile = False
            LOGGER.debug(f'Cannot update testfile {self.testfile}, some commands produced non-zero exit codes')
            return

        actual_output_texts = set()
        for subresult in self.subresults:
            self.updated_testfile = False
            actual_output_texts.add(subresult.actual_output_text)

        if len(actual_output_texts) > 1:
            LOGGER.debug(f'Cannot update testfile {self.testfile}, different commands produced different outputs')
            return

        actual_output_text, = list(actual_output_texts)

        with self.testfile.open('wt') as f:
            print(self.setup_text, file=f)
            print('<<<', file=f)
            print(self.input_text, file=f)
            print('>>>', file=f)
            print(actual_output_text, file=f)

        self.updated_testfile = True

    @staticmethod
    def summarize_results(results: Iterable['TestResult']) -> str:
        result_lines: List[str] = []
        result_lines.append('')
        summaries: Dict[str, List['TestResult']] = defaultdict(lambda: list())
        for result in results:
            summaries[str(result)].append(result)
        for summary, results in sorted(summaries.items(), key=lambda x: x[0]):
            plural = 's' if len(results) > 1 else ''
            testfile_names = format_testfiles(result.testfile for result in results)
            result_lines.append(f'Testfile{plural} {testfile_names}:')
            result_lines.append('')
            for line in summary.splitlines():
                result_lines.append(f'  {line}')
            result_lines.append('')
        return '\n'.join(result_lines)

    def summarize(self) -> str:
        result_lines: List[str] = []
        result_lines.append('')
        for line_number, line in enumerate(str(self).splitlines()):
            if line_number == 0:
                result_lines.append(f'Testfile {format_testfile(self.testfile)}:')
                result_lines.append('')
            else:
                result_lines.append(f'  {line}')
        result_lines.append('')
        return '\n'.join(result_lines)

    def __str__(self) -> str:
        result_lines: List[str] = []
        if self.subresults_succeeded:
            result_lines.append('Success')
        else:
            if not self.subresults_exited_successfully:
                result_lines.append('Some commands produced non-zero exit codes:')
                exit_codes: Dict[int, List[TestSubResult]] = defaultdict(lambda: list())
                for subresult in self.subresults:
                    exit_codes[subresult.exit_code].append(subresult)
                for exit_code, subresults in sorted(exit_codes.items(), key=lambda x: x[0]):
                    command_texts = format_commands(subresult.test_parameters.command for subresult in subresults)
                    plural = 's' if len(subresults) > 1 else ''
                    first_subresult, *_ = subresults
                    if first_subresult.exited_successfully:
                        result_lines.append(f'- Command{plural} {command_texts} exited successfully.')
                    else:
                        result_lines.append(f'- Command{plural} {command_texts} produced exit code {exit_code}.')
                result_lines.append('')
            if not self.subresult_outputs_match:
                result_lines.append('Some commands produced unexpected outputs:')
                diffs: Dict[OutputTextDiff, List[TestSubResult]] = defaultdict(lambda: list())
                for subresult in self.subresults:
                    diffs[subresult.output_diff].append(subresult)
                for diff, subresults in sorted(diffs.items(), key=lambda x: x[0]):
                    command_texts = format_commands(subresult.test_parameters.command for subresult in subresults)
                    plural = 's' if len(subresults) > 1 else ''
                    first_subresult, *_ = subresults
                    if first_subresult.output_matches:
                        result_lines.append(f'- Command{plural} {command_texts} produced expected output.')
                    else:
                        result_lines.append(f'- Command{plural} {command_texts} produced unexpected output with the following diff:')
                        result_lines.append('')
                        for line in diff.splitlines():
                            result_lines.append(f'  {line}')
                        result_lines.append('')
                if result_lines[-1]:  # Make sure that we don't produce double blank lines in the output.
                    result_lines.append('')
                if self.updated_testfile is not None:
                    if self.updated_testfile:
                        result_lines.append('We successfully updated the testfile.')
                    else:
                        result_lines.append('We tried to update the testfile and failed.')
        if result_lines[-1]:  # Make sure that we don't produce a blank line at the end of the output.
            result_lines.pop()
        return '\n'.join(result_lines)

    def __bool__(self) -> bool:
        return self.updated_testfile or self.subresults_succeeded


class BatchResult:
    def __init__(self, testfile_batch: TestFileBatch, test_parameters: TestParameters, temporary_directory: Path,
                 test_process: CompletedProcess) -> None:
        self.testfile_batch = testfile_batch
        assert len(self) >= 1
        self.test_parameters = test_parameters
        self.temporary_directory = temporary_directory
        self.exit_code = test_process.returncode

        # If test succeeded, remove temporary directory.
        if self:
            rmtree(self.temporary_directory)
            self.temporary_directory = None

    @cached_property
    def subresults(self) -> Tuple[TestSubResult, ...]:
        subresult_list: List[TestSubResult] = []

        if len(self.actual_output_texts) == len(self):  # If all testfiles produced output, return subresults.
            for testfile_number, testfile in enumerate(self.testfile_batch):
                subresult = TestSubResult(self, testfile_number)
                subresult_list.append(subresult)
        elif len(self) > 1:  # If some testfiles did not produce output and the batch is larger than one, bisect the batch.
            read_testfile_results, *remaining_parameters = self.test_parameters
            first_testfile_subbatch = self.testfile_batch[:len(self) // 2]
            second_testfile_subbatch = self.testfile_batch[len(self) // 2:]
            first_read_testfile_results = read_testfile_results[:len(self) // 2]
            second_read_testfile_results = read_testfile_results[len(self) // 2:]
            first_test_parameters = TestParameters(first_read_testfile_results, *remaining_parameters)
            second_test_parameters = TestParameters(second_read_testfile_results, *remaining_parameters)
            first_subresults = self.__class__.run_test_batch_with_parameters(first_testfile_subbatch, first_test_parameters)
            second_subresults = self.__class__.run_test_batch_with_parameters(second_testfile_subbatch, second_test_parameters)
            subresult_list = list(chain(first_subresults, second_subresults))
        else:  # If some testfiles did not produce output and the batch contains a single testfile, return the result.
            testfile_number = 0
            subresult = TestSubResult(self, testfile_number)
            subresult_list.append(subresult)

        subresults = tuple(subresult_list)
        return subresults

    @cached_property
    def actual_output_texts(self) -> Tuple[OutputText, ...]:
        try:
            actual_output_file = self.temporary_directory / TEST_ACTUAL_OUTPUT_FILENAME
            with actual_output_file.open('rt') as f:
                actual_output_text = f.read()
            actual_output_texts = split_batch_output_text(actual_output_text)
            actual_output_texts = tuple(actual_output_texts)
            for actual_output_text_number, actual_output_text in enumerate(actual_output_texts):
                actual_output_filename = TEST_ACTUAL_OUTPUT_FILENAME_FORMAT.format(actual_output_text_number)
                actual_output_file = self.temporary_directory / actual_output_filename
                with actual_output_file.open('wt') as f:
                    for line in actual_output_text.splitlines():
                        print(line, file=f)
            assert len(actual_output_texts) <= len(self)
            return actual_output_texts
        except IOError:
            return tuple()  # We have already deleted temporary directory, or no output was produced due to an error.

    def __len__(self):
        return len(self.testfile_batch)

    def __iter__(self):
        return iter(self.subresults)

    def __bool__(self) -> bool:
        return all(self)

    @classmethod
    def run_test_batch_with_parameters(cls, testfile_batch: TestFileBatch, test_parameters: TestParameters) -> Iterable[TestSubResult]:
        read_testfile_results, tex_format, template, command = test_parameters

        # Create a temporary directory.
        temporary_directory = Path(mkdtemp())

        # Copy support files.
        for support_file in SUPPORT_DIRECTORY.glob('*'):
            copyfile(support_file, temporary_directory / support_file.name)

        # Create test file.
        test_texts: List[str] = []

        # Create test file fragment with header.
        with (template / TEMPLATE_HEAD_FILENAME).open('rt') as f:
            head_text = f.read()
        test_texts.append(head_text)

        for testfile_number, (testfile, read_testfile_result) in enumerate(zip_equal(testfile_batch, read_testfile_results)):
            setup_text, input_text, expected_output_text = read_testfile_result

            test_setup_filename = TEST_SETUP_FILENAME_FORMAT.format(testfile_number)
            test_input_filename = TEST_INPUT_FILENAME_FORMAT.format(testfile_number)
            test_expected_output_filename = TEST_EXPECTED_OUTPUT_FILENAME_FORMAT.format(testfile_number)

            # Create testfile-specific support files.
            with (temporary_directory / test_setup_filename).open('wt') as f:
                print(setup_text, file=f)

            with (temporary_directory / test_input_filename).open('wt') as f:
                print(input_text, file=f)

            with (temporary_directory / test_expected_output_filename).open('wt') as f:
                print(expected_output_text, file=f)

            # Create testfile-specific test file fragments.
            body_text = run_m4(
                template / TEMPLATE_BODY_FILENAME,
                cwd=temporary_directory,
                TEST_SETUP_FILENAME=test_setup_filename,
                TEST_INPUT_FILENAME=test_input_filename)
            test_texts.append(body_text)

        # Create test file fragment with footer.
        with (template / TEMPLATE_FOOT_FILENAME).open('rt') as f:
            foot_text = f.read()
        test_texts.append(foot_text)

        test_text = '\n'.join(test_text.strip('\r\n') for test_text in test_texts)
        with (temporary_directory / TEST_FILENAME).open('wt') as f:
            print(test_text, file=f)

        # Run test.
        test_process = run(command, cwd=temporary_directory, capture_output=True)

        # Extract test output.
        try:
            actual_output_file = temporary_directory / TEST_OUTPUT_FILENAME
            actual_output_text = read_test_output_from_tex_log_file(actual_output_file)
            with (temporary_directory / TEST_ACTUAL_OUTPUT_FILENAME).open('wt') as f:
                print(actual_output_text, file=f)
        except IOError as e:
            LOGGER.debug(f'Failed to extract test output from log file: {e}')

        # Store test batch result.
        batch_result = BatchResult(testfile_batch, test_parameters, temporary_directory, test_process)
        yield from batch_result

    @classmethod
    def run_test_batch(cls, testfile_batch: TestFileBatch) -> List[TestResult]:

        # Run the test for all different test parameters.
        all_test_subresults_by_parameters: List[List[TestSubResult]] = []
        for test_parameters in get_test_parameters(testfile_batch):
            subresults = list(cls.run_test_batch_with_parameters(testfile_batch, test_parameters))
            all_test_subresults_by_parameters.append(subresults)

        # For each testfile in the batch, create a test result
        all_test_subresults_by_testfiles = transpose_rectangle(all_test_subresults_by_parameters)
        test_results = list(map(TestResult, all_test_subresults_by_testfiles))
        return test_results


@cache
def get_tex_formats() -> Tuple[TeXFormat, ...]:
    tex_formats: List[TeXFormat] = []
    for tex_format_path in TEMPLATE_DIRECTORY.glob('*/'):
        tex_formats.append(tex_format_path.name)
    return tuple(sorted(tex_formats))


@cache
def get_templates(tex_format: str) -> Tuple[Template, ...]:
    templates: Iterable[Template] = (TEMPLATE_DIRECTORY / tex_format).glob('*/')
    return tuple(sorted(templates))


def run_m4(input_file: Path, cwd: Optional[Path] = None, **variables) -> str:
    with input_file.open('rt') as f:
        input_text = f.read()
    m4_parameters = [f'-D{key}={value}' for key, value in variables.items()]
    m4_process = run(['m4', *m4_parameters], text=True, input=input_text, check=True, capture_output=True, cwd=cwd)
    output_text = m4_process.stdout
    return output_text


@cache
def get_commands(tex_format: str) -> Tuple[Command, ...]:
    commands: List[Command] = []
    commands_file = TEMPLATE_DIRECTORY / tex_format / COMMANDS_FILENAME
    commands_text = run_m4(commands_file, TEST_FILENAME=TEST_FILENAME)
    for command_str in commands_text.splitlines():
        command_str = command_str.strip()
        if command_str:
            command = tuple(command_str.split())
            commands.append(command)
    return tuple(commands)


def read_testfile(testfile: TestFile) -> ReadTestFile:
    input_lines: Dict[str, List] = defaultdict(lambda: list())
    input_part = 'setup'
    with testfile.open('rt') as f:
        for line in f:
            line = line.rstrip('\r\n')
            if input_part == 'setup' and line.strip() == '<<<':
                input_part = 'input'
            elif input_part == 'input' and line.strip() == '>>>':
                input_part = 'expected_output'
            else:
                input_lines[input_part].append(line)

    setup_text = '\n'.join(input_lines['setup'])
    input_text = '\n'.join(input_lines['input'])
    expected_output_text = '\n'.join(input_lines['expected_output'])
    return ReadTestFile(setup_text, input_text, expected_output_text)


def read_test_output_from_tex_log_file(tex_log_file: Path) -> OutputText:
    input_lines: List[str] = []
    input_line_fragments: List[str] = []
    in_test_output = False
    with tex_log_file.open('rt') as f:
        for line in f:
            line = line.rstrip('\r\n')
            if not in_test_output and line.strip() == 'TEST INPUT BEGIN':
                in_test_output = True
            elif in_test_output and line.strip() == 'TEST INPUT END':
                input_lines.append(''.join(input_line_fragments))
                input_line_fragments.clear()
                in_test_output = False
            elif in_test_output:
                input_line_fragments.append(line)

    output_text = '\n'.join(input_lines)
    return output_text


def split_batch_output_text(output_text: OutputText) -> Iterable[OutputText]:
    input_lines: List[str] = []
    in_test_output = False
    for line in output_text.splitlines():
        if not in_test_output and line.strip() == 'documentBegin':
            in_test_output = True
            input_lines.append(line)
        elif in_test_output and line.strip() == 'documentEnd':
            input_lines.append(line)
            output_text = '\n'.join(input_lines)
            input_lines.clear()
            yield output_text
            in_test_output = False
        elif in_test_output:
            input_lines.append(line)


def format_commands(commands: Iterable[Command]) -> str:
    command_texts = [' '.join(command) for command in commands]
    if len(command_texts) > 1:
        command_texts = [f'"{command_text}"' for command_text in command_texts]
    command_texts = ', '.join(command_texts)
    return command_texts


def format_command(command: Command) -> str:
    return format_commands([command])


def format_testfiles(testfiles: Iterable[TestFile]) -> str:
    testfile_texts = list(map(str, testfiles))
    if len(testfile_texts) > 1:
        testfile_texts = [f'"{testfile_text}"' for testfile_text in testfile_texts]
    if len(testfile_texts) > MAX_TESTFILE_NAMES_SHOWN:
        num_hidden = len(testfile_texts) - MAX_TESTFILE_NAMES_SHOWN_COLLAPSED
        testfile_texts = testfile_texts[:MAX_TESTFILE_NAMES_SHOWN_COLLAPSED]
        testfile_texts.append(f'and {num_hidden} others')
    testfile_texts = ', '.join(testfile_texts)
    return testfile_texts


def format_testfile(testfile: TestFile) -> str:
    return format_testfiles([testfile])


def get_test_parameters(testfile_batch: TestFileBatch) -> Iterable[TestParameters]:
    LOGGER.debug(f'Testfiles {format_testfiles(testfile_batch)}')

    # Read testfiles in the batch.
    read_testfile_results: ReadTestFiles = []
    for testfile in testfile_batch:
        read_testfile_result = read_testfile(testfile)
        read_testfile_results.append(read_testfile_result)

    # List TeX formats, templates, and commands.
    for tex_format in get_tex_formats():
        LOGGER.debug(f'  Format {tex_format}')
        for template in get_templates(tex_format):
            LOGGER.debug(f'    Template {template.name}')
            for command in get_commands(tex_format):
                LOGGER.debug(f'      Command {format_command(command)}')
                yield TestParameters(read_testfile_results, tex_format, template, command)


def transpose_rectangle(input_list: Iterable[Iterable[T]]) -> List[List[T]]:
    columns: List[List[T]] = []
    for column in zip_equal(*input_list):
        column = list(column)
        columns.append(column)
    return columns


def run_tests(testfiles: Iterable[TestFile]) -> Iterable[TestResult]:

    testfile_batches: List[TestFileBatch] = list(chunked(testfiles, TESTFILE_BATCH_SIZE))
    LOGGER.debug(f'The testfiles break down into {len(testfile_batches)} batches')

    sequential_results = list(map(BatchResult.run_test_batch, testfile_batches[:1]))  # Populate caches with the first batch.
    with Pool(NUM_PROCESSES) as pool:
        parallel_results = pool.imap(BatchResult.run_test_batch, testfile_batches[1:], chunksize=1)  # Run remaining batches in parallel.
        all_results = chain(sequential_results, parallel_results)
        for results in all_results:
            for result in results:
                yield result


# Command-line interface
@click.command()
@click.argument('testfiles',
                nargs=-1,
                type=click.Path(exists=True, dir_okay=False, file_okay=True),
                required=True)
@click.option('--update-tests/--no-update-tests',
              help='Update testfiles with unexpected results',
              is_flag=True,
              default=UPDATE_TESTS)
@click.option('--fail-fast/--no-fail-fast',
              help='When a test fails, stop immediately',
              is_flag=True,
              default=FAIL_FAST)
def main(testfiles: Iterable[str], update_tests: bool, fail_fast: bool) -> None:

    # Run tests.
    testfiles: List[TestFile] = list(map(Path, testfiles))
    LOGGER.info(f'Running tests for {len(testfiles)} testfiles')

    some_tests_failed = False
    results: List[TestResult] = []
    result_iter = run_tests(testfiles)
    progress_bar = tqdm(result_iter, total=len(testfiles))
    for result in progress_bar:
        if not result:
            some_tests_failed = True
        if not result and update_tests:
            result.try_to_update_testfile()  # Will change bool(result) to True on success.
        if not result and fail_fast:
            progress_bar.close()
            print(result.summarize(), file=sys.stderr)
            sys.exit(1)
        results.append(result)

    # Summarize results.
    if some_tests_failed:
        LOGGER.error('Some tests failed, see the summary below:')
        print(TestResult.summarize_results(results), file=sys.stderr)
        sys.exit(1)
    else:
        LOGGER.info('All tests succeeded!')


if __name__ == '__main__':
    logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s %(message)s')
    main()
