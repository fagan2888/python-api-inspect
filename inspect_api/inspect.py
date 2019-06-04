import ast
import collections
import sys
import re

# some parsings result require more recursion
# pyinstaller is one example
# https://github.com/pyinstaller/pyinstaller/issues/2919
sys.setrecursionlimit(5000)

# https://docs.python.org/3/library/functions.html
BUILTIN_FUNCTIONS = {
    'abs', 'delattr', 'hash', 'memoryview', 'set',
    'all', 'dict', 'help', 'min', 'setattr',
    'any', 'dir', 'hex', 'next', 'slice',
    'ascii', 'divmod', 'id', 'object', 'sorted',
    'bin', 'enumerate', 'input', 'oct', 'staticmethod'
    'bool', 'eval', 'int', 'open', 'str',
    'breakpoint', 'exec', 'isinstance', 'ord', 'sum',
    'bytearray', 'filter', 'issubclass', 'pow', 'super',
    'bytes', 'float', 'iter', 'print', 'tuple',
    'callable', 'format', 'len', 'property', 'type',
    'chr', 'frozenset', 'list', 'range', 'vars',
    'classmethod', 'getattr', 'locals', 'repr', 'zip',
    'compile', 'globals', 'map', 'reversed', '__import__',
    'complex', 'hasattr', 'max', 'round',
}


def extract_name_attribute_path(node):
    """numpy.random.random

    assumes Attribute(Attribute(...(Name()))). returns failing place
    in ast if not
    """
    _node = node
    path = ()

    while isinstance(_node, ast.Attribute):
        path = (_node.attr,) + path
        _node = _node.value

    if isinstance(_node, ast.Name):
        path = (_node.id,) + path
    else:
        raise ValueError(_node)
    return path


def expand_path(path, aliases):
    print(path, aliases)
    if path[0] in aliases:
        return aliases[path[0]] + path[1:]
    print('here', path, path[0] in aliases)
    return path


def is_path_import_match(path, imports):
    for i in range(len(path)):
        if tuple(path[:i+1]) in imports:
            return True
    return False


class ImportVisitor(ast.NodeVisitor):
    def __init__(self):
        self.imports = set()
        self.aliases = {}

    def visit_Import(self, node):
        for name in node.names:
            namespace = tuple(name.name.split('.'))
            if name.asname is None:
                self.imports.add(namespace)
            else:
                self.imports.add(namespace)
                self.aliases[name.asname] = namespace

    def visit_ImportFrom(self, node):
        if node.module is None: # relative import
            return

        partial_namespace = tuple(node.module.split('.'))
        for name in node.names:
            namespace = partial_namespace + (name.name,)
            self.imports.add(namespace)
            self.aliases[name.asname or name.name] = namespace


class APIVisitor(ast.NodeVisitor):
    def __init__(self, aliases, imports):
        self.aliases = aliases
        self.imports = imports

        self.attribute_stats = collections.defaultdict(
            lambda: collections.defaultdict(
                lambda: {
                    'count': 0,
                    'd_count': 0
                }))

        self.function_stats = collections.defaultdict(
            lambda: collections.defaultdict(
                lambda: {
                    'count': 0,
                    'd_count': 0,
                    'n_args': collections.defaultdict(int),
                    'kwargs': collections.defaultdict(int),
                }))

        self.def_function_stats = {
            'count': 0,
            'n_args': collections.defaultdict(int)
        }

    def add_function_stats(self, namespace, path, num_args, keywords, is_decorator):
        self.function_stats[namespace][path]['count'] += 1
        if is_decorator:
            self.function_stats[namespace][path]['d_count'] += 1
        self.function_stats[namespace][path]['n_args'][num_args] += 1
        for keyword in keywords:
            self.function_stats[namespace][path]['kwargs'][keyword] += 1

    def add_attribute_stats(self, namespace, path, is_decorator):
        self.attribute_stats[namespace][path]['count'] += 1
        if is_decorator:
            self.attribute_stats[namespace][path]['d_count'] += 1

    def add_def_function_stats(self, num_args):
        self.def_function_stats['count'] += 1
        self.def_function_stats['n_args'][num_args] += 1

    def visit_Name(self, node, is_decorator=False):
        self.visit_Attribute(node, is_decorator=is_decorator)

    def visit_FunctionDef(self, node):
        num_args = len(node.args.args) + len(node.args.kwonlyargs)
        self.add_def_function_stats(num_args)

        for stmt in node.body:
            self.visit(stmt)

        for _node in node.decorator_list:
            if isinstance(_node, (ast.Attribute, ast.Name)):
                self.visit_Attribute(_node, is_decorator=True)
            elif isinstance(_node, ast.Call):
                self.visit_Call(_node, is_decorator=True)

    def visit_Attribute(self, node, is_decorator=False):
        try:
            path = extract_name_attribute_path(node)
            path = expand_path(path, self.aliases)

            if is_path_import_match(path, self.imports):
                self.add_attribute_stats(path[0], path, is_decorator)
        except ValueError as e:
            # visit non matching part of attribute
            self.visit(e.args[0])
            return

    def visit_Call(self, node, is_decorator=False):
        if not isinstance(node.func, (ast.Attribute, ast.Name)):
            return

        # functions statistics
        num_args = len(node.args) + len(node.keywords)
        keywords = {k.arg for k in node.keywords}

        try:
            path = extract_name_attribute_path(node.func)
            path = expand_path(path, self.aliases)

            if len(path) == 1 and path[0] in BUILTIN_FUNCTIONS:
                base_namespace = '__builtins__'
                self.add_function_stats(base_namespace, path, num_args, keywords, is_decorator)
            elif is_path_import_match(path, self.imports):
                self.add_function_stats(path[0], path, num_args, keywords, is_decorator)
        except ValueError as e:
            # visit non matching part of attribute
            self.visit(e.args[0])
            return

        # visit each call argument
        for arg in node.args:
            self.visit(arg)


def inspect_file_contents(filename, contents):
    if filename.endswith('.py'):
        lines = contents.split(b'\n')
        num_newlines = len(lines)

        comment_regex = re.compile(b'^\s*#.*$')
        num_comment_lines = 0

        whitespace_regex = re.compile(b'^\s*$')
        num_whitespace_lines = 0

        min_line_length = 99999999
        max_line_length = 0
        avg_line_length = 0.0

        for line in lines:
            if line != b'':
                min_line_length = min(min_line_length, len(line))

            if comment_regex.match(line):
                num_comment_lines += 1
            elif whitespace_regex.match(line):
                num_whitespace_lines += 1

            max_line_length = max(max_line_length, len(line))
        avg_line_length = (len(contents) - num_newlines) / num_newlines

        stats = {
            'contents': {
                'num_newlines': num_newlines,
                'num_whitespace_lines': num_whitespace_lines,
                'num_comment_lines': num_comment_lines,
                'min_line_length': min_line_length,
                'max_line_length': max_line_length,
                'avg_line_length': avg_line_length
            }
        }
    elif filename.endswith('.ipynb'):
        stats = {'contents': {}}
    return stats


def inspect_file_ast(file_ast):
    """Record function calls and counts for all absolute namespaces

    """
    import_visitor = ImportVisitor()
    import_visitor.visit(file_ast)

    api_visitor = APIVisitor(
        imports=import_visitor.imports,
        aliases=import_visitor.aliases)
    api_visitor.visit(file_ast)

    return {
        'function': api_visitor.function_stats,
        'def_function': api_visitor.def_function_stats,
        'attribute': api_visitor.attribute_stats
    }
