"""
dbsake.mysqldumpsplit
~~~~~~~~~~~~~~~~~~~~~

Command to split a mysqldump file into constituent parts

"""
import itertools
import logging
import os
import re
import sys

from dbsake import baker

def cmd_to_ext(cmd):
    extensions = dict(gzip='.gz',
                      pigz='.gz',
                      bzip2='.bz2',
                      pbzip2='.bz2',
                      lzop='.lzo',
                      xz='.xz',
                      lzma='.lzma')
    name = cmd.split()[0]
    return extensions.get(name, '')

def output(cmd, name, iterable, mode='wb'):
    from dbsake.util import stream_command
    ext = cmd_to_ext(cmd)
    with open(name + ext, mode) as fileobj:
        with stream_command(cmd, stdout=fileobj) as process:
            for item in iterable:
                process.stdin.write(item)

@baker.command(name='split-mysqldump',
               shortopts=dict(target="t",
                              directory="C",
                              filter_command="f"),
               params=dict(target="MySQL version target (default 5.5)",
                           directory="Directory to output to (default .)",
                           filter_command="Command to filter output through"
                                          "(default gzip -1)"))
def split_mysqldump(target='5.5',
                    directory='.',
                    filter_command='gzip -1',
                    regex='.*'):
    """Split mysqldump output into separate files"""
    from dbsake.util import path, stream_command
    from dbsake.mysqldumpsplit.parser import MySQLDumpParser, extract_identifier
    from dbsake.mysqldumpsplit.defer import extract_create_table, split_indexes
    defer_indexes = False
    defer_constraints = False
    cmd = filter_command
    if target == '5.5':
        defer_indexes = True
    elif target in ('5.6', '5.7'):
        defer_indexes = True
        defer_constraints = True
    else:
        logging.warn("Unknown target version '%s'", target)
        logging.warn("Indexes will not be deferred")
    if path.makedirs(directory, exist_ok=True):
        logging.info("Created output directory '%s'",
                os.path.abspath(directory))
    stream = sys.stdin
    parser = MySQLDumpParser(stream)
    header = None
    post_data = None
    table_count = 0
    database_count = 0
    view_count = 0
    filter_cre = re.compile(regex)
    logging.debug("Compiled regex %s", regex)
    for section_type, iterator in parser.sections:
        if section_type == 'replication_info':
            name = os.path.join(directory, 'replication_info.sql')
            data = itertools.chain([header], iterator)
            output(cmd, name, data)
        elif section_type == 'schema':
            lines = list(iterator)
            db = extract_identifier(lines[1])
            path.makedirs(os.path.join(directory, db), exist_ok=True)
            data = itertools.chain([header], lines)
            output(cmd, os.path.join(directory, db, 'create.sql'), data)
            database_count += 1
        elif section_type == 'schema_routines':
            data = itertools.chain([header], iterator)
            name = os.path.join(directory, db, 'routines.sql')
            output(cmd, name, data)
        elif section_type == 'schema_events':
            data = itertools.chain([header], iterator)
            output(cmd, os.path.join(directory, db, 'events.sql'), data)
        elif section_type in ('table_definition',):
            lines = list(iterator)
            table = extract_identifier(lines[1])
            name = os.path.join(directory, db, table + '.schema.sql')
            table_definition_data = ''.join(lines)
            table_ddl = extract_create_table(table_definition_data)
            if defer_indexes and 'ENGINE=InnoDB' in table_ddl:
                alter_table, create_table = split_indexes(table_ddl,
                                                          defer_constraints)
                if alter_table:
                    if not defer_constraints:
                        info = "indexes"
                    else:
                        info = "indexes and constraints"
                    logging.info("Deferring %s for %s.%s (%s)", info, db,
                            table, name)
                    table_definition_data = table_definition_data.replace(table_ddl, create_table)
                    post_data = alter_table
            data = itertools.chain([header], table_definition_data)
            if filter_cre.search(name):
                output(cmd, name, data)
                table_count += 1
            else:
                logging.debug("No regex match on '%s'", name)
                for line in data: pass
        elif section_type == 'table_data':
            comments = [next(iterator) for _ in xrange(3)]
            table = extract_identifier(comments[1])
            name = os.path.join(directory, db, table + '.data.sql')
            data = itertools.chain([header], comments, iterator)
            if post_data:
                post_data_header = "\n".join([
                    "",
                    "--",
                    "-- InnoDB Fast Index Creation (generated by dbsake)",
                    "--",
                    "",
                    "",
                ])
                data = itertools.chain(data, [post_data_header], [post_data], ["\n"])
                logging.info("Injecting deferred index creation %s", name)
                logging.debug("%s", "\n".join([post_data_header, post_data]))
                post_data = None
            if filter_cre.search(name):
                output(cmd, name, data)
            else:
                logging.debug("No regex match on '%s'", name)
                for line in data:
                    pass
        elif section_type == 'header':
            header = ''.join(list(iterator))
            match = re.search('Database: (?P<schema>.*)$', header, re.M)
            if match and match.group('schema'):
                db = match.group('schema')
                path.makedirs(os.path.join(directory, db), exist_ok=True)
                database_count += 1
        elif section_type in ('view_temporary_definition',
                              'view_definition'):
            name = os.path.join(directory, db, 'views.sql')
            
            if filter_cre.search(name):
                if view_count == 0:
                    # no views have been written yet
                    # truncate the file, if it necessary
                    open(name, 'wb').close()
                output(cmd, name, iterator, mode='ab')
                view_count += 1
            else:
                logging.debug("No regex match on '%s'", name)
                for line in iterator:
                    pass
        else:
            logging.debug("Skipping section type: %s", section_type)
            # drain iterator, so we can continue
            for line in iterator:
                continue
    logging.info("Split input into %d database(s) %d table(s) and %d view(s)",
                 database_count, table_count, view_count)
    return 0
