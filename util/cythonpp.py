#!/usr/bin/python
# Copyright (C) 2011-2012 Denis Bilenko (http://denisbilenko.com)
import sys
import os
import re
import traceback
import datetime
import pipes
import difflib
from hashlib import md5

if sys.version_info >= (3, 0):
    exec("def do_exec(co, loc): exec(co, loc)\n")
else:
    exec("def do_exec(co, loc): exec co in loc\n")

_ex = lambda: sys.exc_info()[1]


CYTHON = os.environ.get('CYTHON') or 'cython'
DEBUG = False
WRITE_OUTPUT = False

# Parameter name in macros must match this regex:
param_name_re = re.compile('^[a-zA-Z_]\w*$')

# First line of a definition of a new macro:
define_re = re.compile(r'^#define\s+([a-zA-Z_]\w*)(\((?:[^,)]+,)*[^,)]+\))?\s+(.*)$')

# Conditional directive:
condition_re = re.compile(r'^#(ifdef\s+.+|if\s+.+|else\s*|endif\s*)$')


def match_condition(line):
    line = line.strip()
    if line.endswith(':'):
        return None
    return condition_re.match(line)

newline_token = ' <cythonpp.py: REPLACE WITH NEWLINE!> '


def process_filename(filename, output_filename=None):
    """Process the .ppyx file with preprocessor and compile it with cython.

    The algorithm is as following:

        1) Identify all possible preprocessor conditions in *filename*.
        2) Run preprocess_filename(*filename*) for each of these conditions.
        3) Process the output of preprocessor with Cython (as many times as
           there are different sources generated for different preprocessor
           definitions.
        4) Merge the output of different Cython runs using preprocessor conditions
           identified in (1).
    """
    if output_filename is None:
        output_filename = filename.rsplit('.', 1)[0] + '.c'

    pyx_filename = filename.rsplit('.', 1)[0] + '.pyx'
    assert pyx_filename != filename

    timestamp = str(datetime.datetime.now().replace(microsecond=0))
    banner = 'Generated by cythonpp.py on %s' % timestamp
    py_banner = '# %s\n' % banner

    preprocessed = {}
    for configuration in get_configurations(filename):
        preprocessed[configuration] = preprocess_filename(filename, Config(configuration))
    preprocessed[None] = preprocess_filename(filename, None)

    preprocessed = expand_to_match(list(preprocessed.items()))
    reference_pyx = preprocessed.pop(None)

    sources = []

    counter = 0
    for configuration, lines in sorted(preprocessed.items()):
        counter += 1
        value = ''.join(lines)
        sourcehash = md5(value.encode("utf-8")).hexdigest()
        comment = format_tag(set(configuration))
        atomic_write(pyx_filename, py_banner + value)
        if WRITE_OUTPUT:
            atomic_write(pyx_filename + '.%s' % counter, '# %s (%s)\n%s' % (banner, comment, value))
        output = run_cython(pyx_filename, sourcehash, output_filename, banner, comment)
        if WRITE_OUTPUT:
            atomic_write(output_filename + '.%s' % counter, output)
        sources.append(attach_tags(output, configuration))

    sys.stderr.write('Generating %s ' % output_filename)
    result = generate_merged(output_filename, sources)
    atomic_write(output_filename, result)
    sys.stderr.write('%s bytes\n' % len(result))

    if filename != pyx_filename:
        log('Saving %s', pyx_filename)
        atomic_write(pyx_filename, py_banner + ''.join(reference_pyx))


def generate_merged(output_filename, sources):
    result = []
    for line in produce_preprocessor(merge(sources)):
        result.append(line.replace(newline_token, '\n'))
    return ''.join(result)


def preprocess_filename(filename, config):
    """Process given .ppyx file with preprocessor.

    This does the following
        1) Resolves "#if"s and "#ifdef"s using config
        2) Expands macro definitions (#define)
    """
    linecount = 0
    current_name = None
    definitions = {}
    result = []
    including_section = []
    for line in open(filename):
        linecount += 1
        rstripped = line.rstrip()
        stripped = rstripped.lstrip()
        try:
            if current_name is not None:
                name = current_name
                value = rstripped
                if value.endswith('\\'):
                    value = value[:-1].rstrip()
                else:
                    current_name = None
                definitions[name]['lines'].append(value)
            else:
                if not including_section or including_section[-1]:
                    m = define_re.match(stripped)
                else:
                    m = None
                if m is not None:
                    name, params, value = m.groups()
                    value = value.strip()
                    if value.endswith('\\'):
                        value = value[:-1].rstrip()
                        current_name = name
                    definitions[name] = {'lines': [value]}
                    if params is None:
                        dbg('Adding definition for %r', name)
                    else:
                        definitions[name]['params'] = parse_parameter_names(params)
                        dbg('Adding definition for %r: %s', name, definitions[name]['params'])
                else:
                    m = match_condition(stripped)
                    if m is not None and config is not None:
                        if stripped == '#else':
                            if not including_section:
                                raise SyntaxError('unexpected "#else"')
                            if including_section[-1]:
                                including_section.pop()
                                including_section.append(False)
                            else:
                                including_section.pop()
                                including_section.append(True)
                        elif stripped == '#endif':
                            if not including_section:
                                raise SyntaxError('unexpected "#endif"')
                            including_section.pop()
                        else:
                            including_section.append(config.is_condition_true(stripped))
                    else:
                        if including_section and not including_section[-1]:
                            pass  # skip this line because last "#if" was false
                        else:
                            if stripped.startswith('#'):
                                # leave comments as is
                                result.append(Str_sourceline(line, linecount - 1))
                            else:
                                lines = expand_definitions(line, definitions).split('\n')
                                if lines and not lines[-1]:
                                    del lines[-1]
                                lines = [x + '\n' for x in lines]
                                lines = [Str_sourceline(x, linecount - 1) for x in lines]
                                result.extend(lines)
        except BaseException:
            ex = _ex()
            log('%s:%s: %s', filename, linecount, ex)
            if type(ex) is SyntaxError:
                sys.exit(1)
            else:
                raise
    return result


def merge(sources):
    r"""Merge different sources into a single one. Each line of the result
    is a subclass of string that maintains the information for each configuration
    it should appear in the result.

    >>> src1 = attach_tags('hello\nworld\n', set([('defined(hello)', True), ('defined(world)', True)]))
    >>> src2 = attach_tags('goodbye\nworld\n', set([('defined(hello)', False), ('defined(world)', True)]))
    >>> src3 = attach_tags('hello\neveryone\n', set([('defined(hello)', True), ('defined(world)', False)]))
    >>> src4 = attach_tags('goodbye\neveryone\n', set([('defined(hello)', False), ('defined(world)', False)]))
    >>> from pprint import pprint
    >>> pprint(merge([src1, src2, src3, src4]))
    [Str('hello\n', [set([('defined(hello)', True)])]),
     Str('goodbye\n', [set([('defined(hello)', False)])]),
     Str('world\n', [set([('defined(world)', True)])]),
     Str('everyone\n', [set([('defined(world)', False)])])]
    """
    if len(sources) <= 1:
        return [Str(str(x), simplify_tags(x.tags)) for x in sources[0]]
    return merge([list(_merge(sources[0], sources[1]))] + sources[2:])


def _merge(a, b):
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == 'equal':
            for line_a, line_b in zip(a[i1:i2], b[j1:j2]):
                tags = getattr(line_a, 'tags', []) + getattr(line_b, 'tags', [])
                yield Str(line_a, tags)
        else:
            for line in a[i1:i2]:
                yield line
            for line in b[j1:j2]:
                yield line


def expand_to_match(items):
    """Insert empty lines so that all sources has matching line numbers for the same code"""
    cfg2newlines = {}  # maps configuration -> list
    for configuration, lines in items:
        cfg2newlines[configuration] = []

    maxguard = 2 ** 30
    while True:
        minimalsourceline = maxguard
        for configuration, lines in items:
            if lines:
                minimalsourceline = min(minimalsourceline, lines[0].sourceline)
        if minimalsourceline == maxguard:
            break

        for configuration, lines in items:
            if lines and lines[0].sourceline <= minimalsourceline:
                cfg2newlines[configuration].append(lines[0])
                del lines[0]

        number_of_lines = max(len(x) for x in list(cfg2newlines.values()))

        for newlines in list(cfg2newlines.values()):
            add = (number_of_lines - len(newlines))
            newlines.extend(['\n'] * add)

    return cfg2newlines


def produce_preprocessor(iterable):

    current_line = [0]

    def wrap(line, log=True):
        current_line[0] += 1
        dbg('%5d: %s', current_line[0], repr(str(line))[1:-1])
        return line

    state = None
    for line in iterable:
        key = line.tags or None

        if key == state:
            yield wrap(line, key)
        else:
            if exact_reverse(key, state):
                yield wrap('#else /* %s */\n' % format_tags(state))
            else:
                if state:
                    yield wrap('#endif /* %s */\n' % format_tags(state))
                if key:
                    yield wrap('#if %s\n' % format_tags(key))
            yield wrap(line, key)
            state = key
    if state:
        yield wrap('#endif /* %s */\n' % format_tags(state))


def exact_reverse(tags1, tags2):
    if not tags1:
        return
    if not tags2:
        return
    if not isinstance(tags1, list):
        raise TypeError(repr(tags1))
    if not isinstance(tags2, list):
        raise TypeError(repr(tags2))
    if len(tags1) == 1 and len(tags2) == 1:
        tag1 = tags1[0]
        tag2 = tags2[0]
        assert isinstance(tag1, set), tag1
        assert isinstance(tag2, set), tag2
        if len(tag1) == 1 and len(tag2) == 1:
            tag1 = list(tag1)[0]
            tag2 = list(tag2)[0]
            if tag1[0] == tag2[0]:
                return sorted([tag1[1], tag2[1]]) == [False, True]


def format_cond(cond):
    if isinstance(cond, tuple) and len(cond) == 2 and isinstance(cond[-1], bool):
        pass
    else:
        raise TypeError(repr(cond))
    if cond[1]:
        return cond[0]
    else:
        return '!' + cond[0]


def format_tag(tag):
    if not isinstance(tag, set):
        raise TypeError(repr(tag))
    return ' && '.join([format_cond(x) for x in sorted(tag)])


def format_tags(tags):
    if not isinstance(tags, list):
        raise TypeError(repr(tags))
    return ' || '.join('(%s)' % format_tag(x) for x in tags)


def attach_tags(text, tags):
    result = [x for x in text.split('\n')]
    if result and not result[-1]:
        del result[-1]
    return [Str(x + '\n', set(tags)) for x in result]


def is_tags_type(tags):
    if not isinstance(tags, list):
        return False
    for tag in tags:
        if not isinstance(tag, set):
            return False
        for item in tag:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bool) and isinstance(item[0], str):
                pass
            else:
                raise TypeError('Invalid item: %r\n%s' % (item, tags))
    return True


class Str(str):
    """This is a string subclass that has a set of tags attached to it.

    Used for merging the outputs.
    """

    def __new__(cls, string, tags):
        if not isinstance(string, str):
            raise TypeError('string must be str: %s' % (type(string), ))
        if isinstance(tags, set):
            tags = [tags]
        if not is_tags_type(tags):
            raise TypeError('tags must be a list of sets of 2-tuples: %r' % (tags, ))
        self = str.__new__(cls, string)
        self.tags = tags
        return self

    def __repr__(self):
        return '%s(%s, %r)' % (self.__class__.__name__, str.__repr__(self), self.tags)

    def __add__(self, other):
        if not isinstance(other, str):
            raise TypeError
        return self.__class__(str.__add__(self, other), self.tags)

    def __radd__(self, other):
        if not isinstance(other, str):
            raise TypeError
        return self.__class__(str.__add__(other, self), self.tags)

    methods = ['__getslice__', '__getitem__', '__mul__', '__rmod__', '__rmul__',
               'join', 'replace', 'upper', 'lower']

    for method in methods:
        do_exec('''def %s(self, *args):
    return self.__class__(str.%s(self, *args), self.tags)''' % (method, method), locals())


def simplify_tags(tags):
    """
    >>> simplify_tags([set([('defined(world)', True), ('defined(hello)', True)]),
    ...                set([('defined(world)', False), ('defined(hello)', True)])])
    [set([('defined(hello)', True)])]
    >>> simplify_tags([set([('defined(LIBEV_EMBED)', True), ('defined(_WIN32)', True)]), set([('defined(LIBEV_EMBED)', True), ('defined(_WIN32)', False)]), set([('defined(_WIN32)', False), ('defined(LIBEV_EMBED)', False)]), set([('defined(LIBEV_EMBED)', False), ('defined(_WIN32)', True)])])
    []
    """
    if not isinstance(tags, list):
        raise TypeError
    for x in tags:
        if not x:
            tags.remove(x)
            return simplify_tags(tags)
    for tag1, tag2 in combinations(tags, 2):
        if tag1 == tag2:
            tags.remove(tag1)
            return simplify_tags(tags)
        for item in tag1:
            reverted_item = reverted(item)
            if reverted_item in tag2:
                tag1_copy = tag1.copy()
                tag1_copy.remove(item)
                tag2_copy = tag2.copy()
                tag2_copy.remove(reverted_item)
                if tag1_copy == tag2_copy:
                    tags.remove(tag1)
                    tags.remove(tag2)
                    tags.append(tag1_copy)
                    return simplify_tags(tags)
    return tags


def reverted(item):
    if not isinstance(item, tuple):
        raise TypeError(repr(item))
    if len(item) != 2:
        raise TypeError(repr(item))
    if item[-1] is True:
        return (item[0], False)
    elif item[-1] is False:
        return (item[0], True)
    raise ValueError(repr(item))


def parse_parameter_names(x):
    assert x.startswith('(') and x.endswith(')'), repr(x)
    x = x[1:-1]
    result = []
    for param in x.split(','):
        param = param.strip()
        if not param_name_re.match(param):
            raise SyntaxError('Invalid parameter name: %r' % param)
        result.append(param)
    return result


def parse_parameter_values(x):
    assert x.startswith('(') and x.endswith(')'), repr(x)
    x = x[1:-1]
    result = []
    for param in x.split(','):
        result.append(param.strip())
    return result


def expand_definitions(code, definitions):
    if not definitions:
        return code
    keys = list(definitions.keys())
    keys.sort(key=lambda x: (-len(x), x))
    keys = '|'.join(keys)

    # This regex defines a macro invocation
    re_macro = re.compile(r'(^|##|[^\w])(%s)(\([^)]+\)|$|##|[^w])' % keys)

    def repl(m):
        token = m.group(2)
        definition = definitions[token]

        params = definition.get('params', [])

        if params:
            arguments = m.group(3)
            if arguments.startswith('(') and arguments.endswith(')'):
                arguments = parse_parameter_values(arguments)
            else:
                arguments = None
            if arguments and len(params) == len(arguments):
                local_definitions = {}
                dbg('Macro %r params=%r arguments=%r source=%r', token, params, arguments, m.groups())
                for key, value in zip(params, arguments):
                    dbg('Adding argument %r=%r', key, value)
                    local_definitions[key] = {'lines': [value]}
                result = expand_definitions('\n'.join(definition['lines']), local_definitions)
            else:
                msg = 'Invalid number of arguments for macro %s: expected %s, got %s'
                msg = msg % (token, len(params), len(arguments or []))
                raise SyntaxError(msg)
        else:
            result = '\n'.join(definition['lines'])
            if m.group(3) != '##':
                result += m.group(3)
        if m.group(1) != '##':
            result = m.group(1) + result
        dbg('Replace %r with %r', m.group(0), result)
        return result

    for _ in range(20000):
        newcode, count = re_macro.subn(repl, code, count=1)
        if code == newcode:
            if count > 0:
                raise SyntaxError('Infinite recursion')
            return newcode
        code = newcode
    raise SyntaxError('Too many substitutions or internal error.')


class Str_sourceline(str):

    def __new__(cls, source, sourceline):
        self = str.__new__(cls, source)
        self.sourceline = sourceline
        return self


def atomic_write(filename, data):
    tmpname = filename + '.tmp.%s' % os.getpid()
    f = open(tmpname, 'w')
    f.write(data)
    f.flush()
    os.fsync(f.fileno())
    f.close()
    os.rename(tmpname, filename)
    dbg('Wrote %s bytes to %s', len(data), filename)


def run_cython(filename, sourcehash, output_filename, banner, comment, cache={}):
    result = cache.get(sourcehash)
    command = '%s -o %s %s' % (CYTHON, pipes.quote(output_filename), pipes.quote(filename))
    if result is not None:
        log('Reusing %s  # %s', command, comment)
        return result
    system(command, comment)
    result = postprocess_cython_output(output_filename, banner)
    cache[sourcehash] = result
    return result


def system(command, comment):
    log('Running %s  # %s', command, comment)
    result = os.system(command)
    if result:
        raise AssertionError('%r failed with code %s' % (command, result))


def postprocess_cython_output(filename, banner):
    # this does a few things:
    # 1) converts multiline C-style (/**/) comments with a single line comment by
    #    replacing \n with newline_token
    # 2) adds our header
    # 3) remove timestamp in cython's header so that different timestamps do not
    #    confuse merger
    result = ['/* %s */\n' % (banner)]

    input = open(filename)
    firstline = input.readline()

    if firstline.strip().lower().startswith('/* generated by cython ') and firstline.strip().endswith('*/'):
        line = firstline.strip().split(' on ', 1)[0]
        result.append(line + ' */')
    else:
        result.append(firstline)

    in_comment = False
    for line in input:

        if line.endswith('\n'):
            line = line[:-1].rstrip() + '\n'

        if in_comment:
            if '*/' in line:
                in_comment = False
                result.append(line)
            else:
                result.append(line.replace('\n', newline_token))
        else:
            if line.lstrip().startswith('/* ') and '*/' not in line:
                line = line.lstrip()  # cython adds space before /* for some reason
                line = line.replace('\n', newline_token)
                result.append(line)
                in_comment = True
            else:
                result.append(line)
    return ''.join(result)


class Config(object):

    def __init__(self, configuration):
        self.conditions = set(configuration)

    def is_condition_true(self, directive):
        if directive.startswith('#if '):
            parameter = directive.split(' ', 1)[1]
        elif directive.startswith('#ifdef '):
            parameter = directive.split(' ', 1)[1]
            parameter = 'defined(%s)' % parameter
        else:
            raise AssertionError('Invalid directive: %r' % directive)
        cond = (parameter, True)
        return cond in self.conditions


def get_conditions(filename):
    conditions = set()
    condition_stack = []
    linecount = 0
    for line in open(filename):
        linecount += 1
        try:
            m = match_condition(line)
            if m is not None:
                split = m.group(1).strip().split(' ', 1)
                directive = split[0].strip()
                if len(split) == 1:
                    parameter = None
                    assert directive in ('else', 'endif'), directive
                else:
                    parameter = split[1].strip()
                    assert directive in ('if', 'ifdef'), directive
                if directive == 'ifdef':
                    directive = 'if'
                    parameter = 'defined(%s)' % parameter
                if directive == 'if':
                    condition_stack.append((parameter, True))
                elif directive == 'else':
                    if not condition_stack:
                        raise SyntaxError('Unexpected "#else"')
                    last_cond, true = condition_stack.pop()
                    assert true is True, true
                    condition_stack.append((last_cond, not true))
                elif directive == 'endif':
                    if not condition_stack:
                        raise SyntaxError('Unexpected "#endif"')
                    condition_stack.pop()
                else:
                    raise AssertionError('Internal error')
            else:
                conditions.add(tuple(condition_stack))
        except BaseException:
            ex = _ex()
            log('%s:%s: %s', filename, linecount, ex)
            if type(ex) is SyntaxError:
                sys.exit(1)
            else:
                raise
    return conditions


def flat_tuple(x):
    result = []
    for item in x:
        for subitem in item:
            result.append(subitem)
    return tuple(result)


def get_selections(items):
    return set([flat_tuple(sorted(set(x))) for x in product(items, repeat=len(items))])


def is_impossible(configuration):
    conds = {}
    for cond, flag in configuration:
        if cond in conds:
            if conds.get(cond) != flag:
                return True
        conds[cond] = flag


def get_configurations(filename):
    conditions = get_conditions(filename)

    configurations = []
    allconds = set()

    for configuration in get_selections(conditions):
        if not is_impossible(configuration):
            configurations.append(configuration)
            for cond, flag in configuration:
                allconds.add(cond)

    result = set()
    for configuration in configurations:
        conds = set(x[0] for x in configuration)
        missing_conds = allconds - conds
        for cond in missing_conds:
            configuration = configuration + ((cond, False), )
        result.add(tuple(sorted(configuration)))

    return result


def log(message, *args):
    try:
        string = message % args
    except Exception:
        try:
            prefix = 'Traceback (most recent call last):\n'
            lines = traceback.format_stack()[:-1]
            error_lines = traceback.format_exc().replace(prefix, '')
            last_length = len(lines[-1].strip().rsplit('    ', 1)[-1])
            last_length = min(80, last_length)
            last_length = max(5, last_length)
            msg = '%s%s    %s\n%s' % (prefix, ''.join(lines), '^' * last_length, error_lines)
            sys.stderr.write(msg)
        except Exception:
            traceback.print_exc()
        try:
            message = '%r %% %r\n\n' % (message, args)
        except Exception:
            pass
        try:
            sys.stderr.write(message)
        except Exception:
            traceback.print_exc()
    else:
        sys.stderr.write(string + '\n')


def dbg(*args):
    if not DEBUG:
        return
    return log(*args)


# itertools is not available on python 2.5
# itertools.combinations has a problem on 2.6.1

def combinations(iterable, r):
    # combinations('ABCD', 2) --> AB AC AD BC BD CD
    # combinations(range(4), 3) --> 012 013 023 123
    pool = tuple(iterable)
    n = len(pool)
    if r > n:
        return
    indices = list(range(r))
    yield tuple(pool[i] for i in indices)
    while True:
        for i in reversed(list(range(r))):
            if indices[i] != i + n - r:
                break
        else:
            return
        indices[i] += 1
        for j in range(i+1, r):
            indices[j] = indices[j-1] + 1
        yield tuple(pool[i] for i in indices)


def product(*args, **kwds):
    # product('ABCD', 'xy') --> Ax Ay Bx By Cx Cy Dx Dy
    # product(range(2), repeat=3) --> 000 001 010 011 100 101 110 111
    pools = tuple(map(tuple, args)) * kwds.get('repeat', 1)
    result = [[]]
    for pool in pools:
        result = [x+[y] for x in result for y in pool]
    for prod in result:
        yield tuple(prod)


if __name__ == '__main__':
    import optparse
    parser = optparse.OptionParser()
    parser.add_option('--debug', action='store_true')
    parser.add_option('--list', action='store_true', help='Show the list of different conditions')
    parser.add_option('--list-cond', action='store_true')
    parser.add_option('--ignore-cond', action='store_true', help='Ignore conditional directives (only expand definitions)')
    parser.add_option('--write-intermediate', action='store_true', help='Save intermediate files produced by preprocessor and Cython')
    parser.add_option('-o', '--output-file', help='Specify name of generated C file')

    options, args = parser.parse_args()
    if len(args) != 1:
        sys.exit('Expected one argument, got %s' % len(args))
    filename = args[0]

    if options.debug:
        DEBUG = True

    if options.write_intermediate:
        WRITE_OUTPUT = True

    run = True

    if options.list_cond:
        run = False
        for x in get_conditions(filename):
            sys.stdout.write('* %s\n' % (x, ))

    if options.list:
        run = False
        for x in get_configurations(filename):
            sys.stdout.write('* %s\n' % (x, ))

    if options.ignore_cond:
        run = False

        class FakeConfig(object):
            def is_condition_true(*args):
                return False

        sys.stdout.write(preprocess_filename(filename, FakeConfig()))

    if run:
        process_filename(filename, options.output_file)
