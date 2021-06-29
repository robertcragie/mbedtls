#!/usr/bin/env python3
"""Generate test data for PSA cryptographic mechanisms.

With no arguments, generate all test data. With non-option arguments,
generate only the specified files.
"""

# Copyright The Mbed TLS Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import os
import posixpath
import re
import sys
from typing import Callable, Dict, FrozenSet, Iterable, Iterator, List, Optional, TypeVar

import scripts_path # pylint: disable=unused-import
from mbedtls_dev import build_tree
from mbedtls_dev import crypto_knowledge
from mbedtls_dev import macro_collector
from mbedtls_dev import psa_storage
from mbedtls_dev import test_case

T = TypeVar('T') #pylint: disable=invalid-name


def psa_want_symbol(name: str) -> str:
    """Return the PSA_WANT_xxx symbol associated with a PSA crypto feature."""
    if name.startswith('PSA_'):
        return name[:4] + 'WANT_' + name[4:]
    else:
        raise ValueError('Unable to determine the PSA_WANT_ symbol for ' + name)

def finish_family_dependency(dep: str, bits: int) -> str:
    """Finish dep if it's a family dependency symbol prefix.

    A family dependency symbol prefix is a PSA_WANT_ symbol that needs to be
    qualified by the key size. If dep is such a symbol, finish it by adjusting
    the prefix and appending the key size. Other symbols are left unchanged.
    """
    return re.sub(r'_FAMILY_(.*)', r'_\1_' + str(bits), dep)

def finish_family_dependencies(dependencies: List[str], bits: int) -> List[str]:
    """Finish any family dependency symbol prefixes.

    Apply `finish_family_dependency` to each element of `dependencies`.
    """
    return [finish_family_dependency(dep, bits) for dep in dependencies]

SYMBOLS_WITHOUT_DEPENDENCY = frozenset([
    'PSA_ALG_AEAD_WITH_AT_LEAST_THIS_LENGTH_TAG', # modifier, only in policies
    'PSA_ALG_AEAD_WITH_SHORTENED_TAG', # modifier
    'PSA_ALG_ANY_HASH', # only in policies
    'PSA_ALG_AT_LEAST_THIS_LENGTH_MAC', # modifier, only in policies
    'PSA_ALG_KEY_AGREEMENT', # chaining
    'PSA_ALG_TRUNCATED_MAC', # modifier
])
def automatic_dependencies(*expressions: str) -> List[str]:
    """Infer dependencies of a test case by looking for PSA_xxx symbols.

    The arguments are strings which should be C expressions. Do not use
    string literals or comments as this function is not smart enough to
    skip them.
    """
    used = set()
    for expr in expressions:
        used.update(re.findall(r'PSA_(?:ALG|ECC_FAMILY|KEY_TYPE)_\w+', expr))
    used.difference_update(SYMBOLS_WITHOUT_DEPENDENCY)
    return sorted(psa_want_symbol(name) for name in used)

# A temporary hack: at the time of writing, not all dependency symbols
# are implemented yet. Skip test cases for which the dependency symbols are
# not available. Once all dependency symbols are available, this hack must
# be removed so that a bug in the dependency symbols proprely leads to a test
# failure.
def read_implemented_dependencies(filename: str) -> FrozenSet[str]:
    return frozenset(symbol
                     for line in open(filename)
                     for symbol in re.findall(r'\bPSA_WANT_\w+\b', line))
_implemented_dependencies = None #type: Optional[FrozenSet[str]] #pylint: disable=invalid-name
def hack_dependencies_not_implemented(dependencies: List[str]) -> None:
    global _implemented_dependencies #pylint: disable=global-statement,invalid-name
    if _implemented_dependencies is None:
        _implemented_dependencies = \
            read_implemented_dependencies('include/psa/crypto_config.h')
    if not all(dep.lstrip('!') in _implemented_dependencies
               for dep in dependencies):
        dependencies.append('DEPENDENCY_NOT_IMPLEMENTED_YET')


class Information:
    """Gather information about PSA constructors."""

    def __init__(self) -> None:
        self.constructors = self.read_psa_interface()

    @staticmethod
    def remove_unwanted_macros(
            constructors: macro_collector.PSAMacroEnumerator
    ) -> None:
        # Mbed TLS doesn't support finite-field DH yet and will not support
        # finite-field DSA. Don't attempt to generate any related test case.
        constructors.key_types.discard('PSA_KEY_TYPE_DH_KEY_PAIR')
        constructors.key_types.discard('PSA_KEY_TYPE_DH_PUBLIC_KEY')
        constructors.key_types.discard('PSA_KEY_TYPE_DSA_KEY_PAIR')
        constructors.key_types.discard('PSA_KEY_TYPE_DSA_PUBLIC_KEY')

    def read_psa_interface(self) -> macro_collector.PSAMacroEnumerator:
        """Return the list of known key types, algorithms, etc."""
        constructors = macro_collector.InputsForTest()
        header_file_names = ['include/psa/crypto_values.h',
                             'include/psa/crypto_extra.h']
        test_suites = ['tests/suites/test_suite_psa_crypto_metadata.data']
        for header_file_name in header_file_names:
            constructors.parse_header(header_file_name)
        for test_cases in test_suites:
            constructors.parse_test_cases(test_cases)
        self.remove_unwanted_macros(constructors)
        constructors.gather_arguments()
        return constructors


def test_case_for_key_type_not_supported(
        verb: str, key_type: str, bits: int,
        dependencies: List[str],
        *args: str,
        param_descr: str = ''
) -> test_case.TestCase:
    """Return one test case exercising a key creation method
    for an unsupported key type or size.
    """
    hack_dependencies_not_implemented(dependencies)
    tc = test_case.TestCase()
    short_key_type = re.sub(r'PSA_(KEY_TYPE|ECC_FAMILY)_', r'', key_type)
    adverb = 'not' if dependencies else 'never'
    if param_descr:
        adverb = param_descr + ' ' + adverb
    tc.set_description('PSA {} {} {}-bit {} supported'
                       .format(verb, short_key_type, bits, adverb))
    tc.set_dependencies(dependencies)
    tc.set_function(verb + '_not_supported')
    tc.set_arguments([key_type] + list(args))
    return tc

class NotSupported:
    """Generate test cases for when something is not supported."""

    def __init__(self, info: Information) -> None:
        self.constructors = info.constructors

    ALWAYS_SUPPORTED = frozenset([
        'PSA_KEY_TYPE_DERIVE',
        'PSA_KEY_TYPE_RAW_DATA',
    ])
    def test_cases_for_key_type_not_supported(
            self,
            kt: crypto_knowledge.KeyType,
            param: Optional[int] = None,
            param_descr: str = '',
    ) -> Iterator[test_case.TestCase]:
        """Return test cases exercising key creation when the given type is unsupported.

        If param is present and not None, emit test cases conditioned on this
        parameter not being supported. If it is absent or None, emit test cases
        conditioned on the base type not being supported.
        """
        if kt.name in self.ALWAYS_SUPPORTED:
            # Don't generate test cases for key types that are always supported.
            # They would be skipped in all configurations, which is noise.
            return
        import_dependencies = [('!' if param is None else '') +
                               psa_want_symbol(kt.name)]
        if kt.params is not None:
            import_dependencies += [('!' if param == i else '') +
                                    psa_want_symbol(sym)
                                    for i, sym in enumerate(kt.params)]
        if kt.name.endswith('_PUBLIC_KEY'):
            generate_dependencies = []
        else:
            generate_dependencies = import_dependencies
        for bits in kt.sizes_to_test():
            yield test_case_for_key_type_not_supported(
                'import', kt.expression, bits,
                finish_family_dependencies(import_dependencies, bits),
                test_case.hex_string(kt.key_material(bits)),
                param_descr=param_descr,
            )
            if not generate_dependencies and param is not None:
                # If generation is impossible for this key type, rather than
                # supported or not depending on implementation capabilities,
                # only generate the test case once.
                continue
            yield test_case_for_key_type_not_supported(
                'generate', kt.expression, bits,
                finish_family_dependencies(generate_dependencies, bits),
                str(bits),
                param_descr=param_descr,
            )
            # To be added: derive

    ECC_KEY_TYPES = ('PSA_KEY_TYPE_ECC_KEY_PAIR',
                     'PSA_KEY_TYPE_ECC_PUBLIC_KEY')

    def test_cases_for_not_supported(self) -> Iterator[test_case.TestCase]:
        """Generate test cases that exercise the creation of keys of unsupported types."""
        for key_type in sorted(self.constructors.key_types):
            if key_type in self.ECC_KEY_TYPES:
                continue
            kt = crypto_knowledge.KeyType(key_type)
            yield from self.test_cases_for_key_type_not_supported(kt)
        for curve_family in sorted(self.constructors.ecc_curves):
            for constr in self.ECC_KEY_TYPES:
                kt = crypto_knowledge.KeyType(constr, [curve_family])
                yield from self.test_cases_for_key_type_not_supported(
                    kt, param_descr='type')
                yield from self.test_cases_for_key_type_not_supported(
                    kt, 0, param_descr='curve')


class StorageKey(psa_storage.Key):
    """Representation of a key for storage format testing."""

    def __init__(self, *, description: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.description = description #type: str

class StorageFormat:
    """Storage format stability test cases."""

    def __init__(self, info: Information, version: int, forward: bool) -> None:
        """Prepare to generate test cases for storage format stability.

        * `info`: information about the API. See the `Information` class.
        * `version`: the storage format version to generate test cases for.
        * `forward`: if true, generate forward compatibility test cases which
          save a key and check that its representation is as intended. Otherwise
          generate backward compatibility test cases which inject a key
          representation and check that it can be read and used.
        """
        self.constructors = info.constructors
        self.version = version
        self.forward = forward

    def make_test_case(self, key: StorageKey) -> test_case.TestCase:
        """Construct a storage format test case for the given key.

        If ``forward`` is true, generate a forward compatibility test case:
        create a key and validate that it has the expected representation.
        Otherwise generate a backward compatibility test case: inject the
        key representation into storage and validate that it can be read
        correctly.
        """
        verb = 'save' if self.forward else 'read'
        tc = test_case.TestCase()
        tc.set_description('PSA storage {}: {}'.format(verb, key.description))
        dependencies = automatic_dependencies(
            key.lifetime.string, key.type.string,
            key.usage.string, key.alg.string, key.alg2.string,
        )
        dependencies = finish_family_dependencies(dependencies, key.bits)
        tc.set_dependencies(dependencies)
        tc.set_function('key_storage_' + verb)
        if self.forward:
            extra_arguments = []
        else:
            flags = []
            # Some test keys have the RAW_DATA type and attributes that don't
            # necessarily make sense. We do this to validate numerical
            # encodings of the attributes.
            # Raw data keys have no useful exercise anyway so there is no
            # loss of test coverage.
            if key.type.string != 'PSA_KEY_TYPE_RAW_DATA':
                flags.append('TEST_FLAG_EXERCISE')
            if 'READ_ONLY' in key.lifetime.string:
                flags.append('TEST_FLAG_READ_ONLY')
            extra_arguments = [' | '.join(flags) if flags else '0']
        tc.set_arguments([key.lifetime.string,
                          key.type.string, str(key.bits),
                          key.usage.string, key.alg.string, key.alg2.string,
                          '"' + key.material.hex() + '"',
                          '"' + key.hex() + '"',
                          *extra_arguments])
        return tc

    def key_for_lifetime(
            self,
            lifetime: str,
    ) -> StorageKey:
        """Construct a test key for the given lifetime."""
        short = lifetime
        short = re.sub(r'PSA_KEY_LIFETIME_FROM_PERSISTENCE_AND_LOCATION',
                       r'', short)
        short = re.sub(r'PSA_KEY_[A-Z]+_', r'', short)
        description = 'lifetime: ' + short
        key = StorageKey(version=self.version,
                         id=1, lifetime=lifetime,
                         type='PSA_KEY_TYPE_RAW_DATA', bits=8,
                         usage='PSA_KEY_USAGE_EXPORT', alg=0, alg2=0,
                         material=b'L',
                         description=description)
        return key

    def all_keys_for_lifetimes(self) -> Iterator[StorageKey]:
        """Generate test keys covering lifetimes."""
        lifetimes = sorted(self.constructors.lifetimes)
        expressions = self.constructors.generate_expressions(lifetimes)
        for lifetime in expressions:
            # Don't attempt to create or load a volatile key in storage
            if 'VOLATILE' in lifetime:
                continue
            # Don't attempt to create a read-only key in storage,
            # but do attempt to load one.
            if 'READ_ONLY' in lifetime and self.forward:
                continue
            yield self.key_for_lifetime(lifetime)

    def key_for_usage_flags(
            self,
            usage_flags: List[str],
            short: Optional[str] = None
    ) -> StorageKey:
        """Construct a test key for the given key usage."""
        usage = ' | '.join(usage_flags) if usage_flags else '0'
        if short is None:
            short = re.sub(r'\bPSA_KEY_USAGE_', r'', usage)
        description = 'usage: ' + short
        key = StorageKey(version=self.version,
                         id=1, lifetime=0x00000001,
                         type='PSA_KEY_TYPE_RAW_DATA', bits=8,
                         usage=usage, alg=0, alg2=0,
                         material=b'K',
                         description=description)
        return key

    def all_keys_for_usage_flags(self) -> Iterator[StorageKey]:
        """Generate test keys covering usage flags."""
        known_flags = sorted(self.constructors.key_usage_flags)
        yield self.key_for_usage_flags(['0'])
        for usage_flag in known_flags:
            yield self.key_for_usage_flags([usage_flag])
        for flag1, flag2 in zip(known_flags,
                                known_flags[1:] + [known_flags[0]]):
            yield self.key_for_usage_flags([flag1, flag2])
        yield self.key_for_usage_flags(known_flags, short='all known')

    def keys_for_type(
            self,
            key_type: str,
            params: Optional[Iterable[str]] = None
    ) -> Iterator[StorageKey]:
        """Generate test keys for the given key type.

        For key types that depend on a parameter (e.g. elliptic curve family),
        `param` is the parameter to pass to the constructor. Only a single
        parameter is supported.
        """
        kt = crypto_knowledge.KeyType(key_type, params)
        for bits in kt.sizes_to_test():
            usage_flags = 'PSA_KEY_USAGE_EXPORT'
            alg = 0
            alg2 = 0
            key_material = kt.key_material(bits)
            short_expression = re.sub(r'\bPSA_(?:KEY_TYPE|ECC_FAMILY)_',
                                      r'',
                                      kt.expression)
            description = 'type: {} {}-bit'.format(short_expression, bits)
            key = StorageKey(version=self.version,
                             id=1, lifetime=0x00000001,
                             type=kt.expression, bits=bits,
                             usage=usage_flags, alg=alg, alg2=alg2,
                             material=key_material,
                             description=description)
            yield key

    def all_keys_for_types(self) -> Iterator[StorageKey]:
        """Generate test keys covering key types and their representations."""
        key_types = sorted(self.constructors.key_types)
        for key_type in self.constructors.generate_expressions(key_types):
            yield from self.keys_for_type(key_type)

    def keys_for_algorithm(self, alg: str) -> Iterator[StorageKey]:
        """Generate test keys for the specified algorithm."""
        # For now, we don't have information on the compatibility of key
        # types and algorithms. So we just test the encoding of algorithms,
        # and not that operations can be performed with them.
        descr = re.sub(r'PSA_ALG_', r'', alg)
        descr = re.sub(r',', r', ', re.sub(r' +', r'', descr))
        usage = 'PSA_KEY_USAGE_EXPORT'
        key1 = StorageKey(version=self.version,
                          id=1, lifetime=0x00000001,
                          type='PSA_KEY_TYPE_RAW_DATA', bits=8,
                          usage=usage, alg=alg, alg2=0,
                          material=b'K',
                          description='alg: ' + descr)
        yield key1
        key2 = StorageKey(version=self.version,
                          id=1, lifetime=0x00000001,
                          type='PSA_KEY_TYPE_RAW_DATA', bits=8,
                          usage=usage, alg=0, alg2=alg,
                          material=b'L',
                          description='alg2: ' + descr)
        yield key2

    def all_keys_for_algorithms(self) -> Iterator[StorageKey]:
        """Generate test keys covering algorithm encodings."""
        algorithms = sorted(self.constructors.algorithms)
        for alg in self.constructors.generate_expressions(algorithms):
            yield from self.keys_for_algorithm(alg)

    def all_test_cases(self) -> Iterator[test_case.TestCase]:
        """Generate all storage format test cases."""
        # First build a list of all keys, then construct all the corresponding
        # test cases. This allows all required information to be obtained in
        # one go, which is a significant performance gain as the information
        # includes numerical values obtained by compiling a C program.
        keys = [] #type: List[StorageKey]
        keys += self.all_keys_for_lifetimes()
        keys += self.all_keys_for_usage_flags()
        keys += self.all_keys_for_types()
        keys += self.all_keys_for_algorithms()
        for key in keys:
            if key.location_value() != 0:
                # Skip keys with a non-default location, because they
                # require a driver and we currently have no mechanism to
                # determine whether a driver is available.
                continue
            yield self.make_test_case(key)


class TestGenerator:
    """Generate test data."""

    def __init__(self, options) -> None:
        self.test_suite_directory = self.get_option(options, 'directory',
                                                    'tests/suites')
        self.info = Information()

    @staticmethod
    def get_option(options, name: str, default: T) -> T:
        value = getattr(options, name, None)
        return default if value is None else value

    def filename_for(self, basename: str) -> str:
        """The location of the data file with the specified base name."""
        return posixpath.join(self.test_suite_directory, basename + '.data')

    def write_test_data_file(self, basename: str,
                             test_cases: Iterable[test_case.TestCase]) -> None:
        """Write the test cases to a .data file.

        The output file is ``basename + '.data'`` in the test suite directory.
        """
        filename = self.filename_for(basename)
        test_case.write_data_file(filename, test_cases)

    TARGETS = {
        'test_suite_psa_crypto_not_supported.generated':
        lambda info: NotSupported(info).test_cases_for_not_supported(),
        'test_suite_psa_crypto_storage_format.current':
        lambda info: StorageFormat(info, 0, True).all_test_cases(),
        'test_suite_psa_crypto_storage_format.v0':
        lambda info: StorageFormat(info, 0, False).all_test_cases(),
    } #type: Dict[str, Callable[[Information], Iterable[test_case.TestCase]]]

    def generate_target(self, name: str) -> None:
        test_cases = self.TARGETS[name](self.info)
        self.write_test_data_file(name, test_cases)

def main(args):
    """Command line entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true',
                        help='List available targets and exit')
    parser.add_argument('targets', nargs='*', metavar='TARGET',
                        help='Target file to generate (default: all; "-": none)')
    options = parser.parse_args(args)
    build_tree.chdir_to_root()
    generator = TestGenerator(options)
    if options.list:
        for name in sorted(generator.TARGETS):
            print(generator.filename_for(name))
        return
    if options.targets:
        # Allow "-" as a special case so you can run
        # ``generate_psa_tests.py - $targets`` and it works uniformly whether
        # ``$targets`` is empty or not.
        options.targets = [os.path.basename(re.sub(r'\.data\Z', r'', target))
                           for target in options.targets
                           if target != '-']
    else:
        options.targets = sorted(generator.TARGETS)
    for target in options.targets:
        generator.generate_target(target)

if __name__ == '__main__':
    main(sys.argv[1:])
