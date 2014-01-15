"""
dbsake.mysqlfrm.binaryfrm
~~~~~~~~~~~~~~~~~~~~~~~~~

Parse binary .frm files

"""

import collections
import datetime
import errno
import itertools
import os
import re

# local imports
from . import charsets
from . import constants
from . import keys
from . import mysqltypes
from . import tablename
from . import util

#: Packed .frm sections and other attributes
PackedFrmData = collections.namedtuple('PackedFrmData',
                                       'path mysql_version keyinfo '
                                       'defaults extrainfo columns')

#: Packed column bytestrings
PackedColumnData = collections.namedtuple('PackedColumnInfo', 
                                          'count null_count names labels metadata '
                                          'defaults comments')

_TableOptions = collections.namedtuple('TableOptions',
                                       'connection engine charset '
                                       'min_rows max_rows avg_row_length '
                                       'pack_keys delay_key_write checksum '
                                       'row_format key_block_size '
                                       'stats_persistent '
                                       'comment partitions')

class TableOptions(_TableOptions):
    def __str__(self):
        return ' '.join(self.attributes())

    def attributes(self):
        if self.connection:
            yield "CONNECTION='{0}'".format(self.connection)
        if self.engine:
            yield "ENGINE={0}".format(self.engine)
        if self.charset:
            yield 'DEFAULT CHARSET={0}'.format(self.charset.name)
            if not self.charset.is_default:
                yield 'COLLATE={0}'.format(self.charset.collation)
        if self.min_rows:
            yield 'MIN_ROWS={0}'.format(self.min_rows)
        if self.max_rows:
            yield 'MAX_ROWS={0}'.format(self.max_rows)
        if self.avg_row_length:
            yield 'AVG_ROW_LENGTH={0}'.format(self.avg_row_length)
        if self.pack_keys is not None:
            yield 'PACK_KEYS={0}'.format(self.pack_keys)
        if self.stats_persistent is not None:
            yield 'STATS_PERSISTENT={0}'.format(self.stats_persistent)
        if self.checksum:
            yield 'CHECKSUM=1'
        if self.delay_key_write:
            yield 'DELAY_KEY_WRITE=1'
        if self.row_format.name != 'DEFAULT':
            yield 'ROW_FORMAT={0}'.format(self.row_format.name)
        if self.key_block_size:
            yield 'KEY_BLOCK_SIZE={0}'.format(self.key_block_size)
        if self.comment:
            yield "COMMENT '{0}'".format(self.comment)
        if self.partitions:
            # Patch any comments in partition_clause so we still have valid SQL
            # This was a fairly recent feature introduced into MySQL to tweak
            # PARTITION BY [LINEAR] KEY
            partitions = re.sub(r'([/][*]!\d+ ALGORITHM = \d+ [*][/])',
                                r'*/ \1 /*!50100', self.partitions)
            yield "\n/*!50100 {0} */".format(partitions)


_Table = collections.namedtuple('Table', 
                                'name charset mysql_version options '
                                'columns keys')

class Table(_Table):
    @property
    def type(self):
        return 'TABLE'

    @classmethod
    def from_data(cls, data, context):
        extrainfo = context.extrainfo
        name = os.path.basename(os.path.splitext(context.path)[0])
        charset = charsets.lookup(data.uint8_at(0x0026))
        mysql_version = MySQLVersion.from_version_id(data.uint32_at(0x0033))
        # various table options encoded in header
        min_rows = data.uint32_at(0x0016)
        max_rows = data.uint32_at(0x0012)
        avg_row_length = data.uint32_at(0x0022)
        row_format = constants.HaRowType(data.uint8_at(0x0028))
        key_block_size = data.uint16_at(0x003e)
        handler_options = constants.HaOption(data.uint16_at(0x001e))

        # items possibly derived from extra section
        if extrainfo.getvalue():
            connection = extrainfo.bytes_prefix16()
            engine = extrainfo.bytes_prefix16()
            partition_info = extrainfo.bytes_prefix32()
            extrainfo.skip(2) # skip null + autopartition flag
        else:
            connection = None
            engine = None
            partition_info = None

        if not engine:
            # legacy_db_type
            engine = constants.LegacyDBType(data.uint8_at(0x0003)).name
        elif engine == 'partition':
            # default_part_db_type 
            # this is underlying storage engine of the partitioned table
            engine = constants.LegacyDBType(data.uint8_at(0x003d)).name

        pack_keys = None
        if handler_options.PACK_KEYS:
            pack_keys = 1
        elif handler_options.NO_PACK_KEYS:
            pack_keys = 0

        stats_persistent = None
        if handler_options.STATS_PERSISTENT:
            stats_persistent = 1
        elif handler_options.NO_STATS_PERSISTENT:
            stats_persistent = 0

        checksum = True if handler_options.CHECKSUM else False
        delay_key_write = bool(handler_options.DELAY_KEY_WRITE)

        return cls(
            name=tablename.filename_to_tablename(name),
            mysql_version=mysql_version,
            charset=charset,
            options=TableOptions(
                        connection=connection,
                        engine=engine,
                        charset=charset,
                        min_rows=min_rows,
                        max_rows=max_rows,
                        avg_row_length=avg_row_length,
                        pack_keys=pack_keys,
                        stats_persistent=stats_persistent,
                        checksum=checksum,
                        delay_key_write=delay_key_write,
                        row_format=row_format,
                        key_block_size=key_block_size,
                        comment=None,
                        partitions=partition_info
                    ),
            columns=(),
            keys=()
        )
            
    def format(self, include_raw_types=False):
        def _fmt_column(column):
            value = str(column)
            if include_raw_types:
                value += ' /* MYSQL_TYPE_%s */' % column.type_code.name
            return value

        g = itertools.chain((_fmt_column(c) for c in self.columns), self.keys)
        parts = [
            u"--",
            u"-- Table structure for table `%s`" % self.name,
            u"-- Created with MySQL Version {0}".format(self.mysql_version),
            u"--",
            u"",
            u"CREATE TABLE `%s` (" % self.name,
            u",\n".join("  %s" % str(name) for name in g),
            u") {0};".format(self.options),
            u""
        ]

        return os.linesep.join(parts)

_Column = collections.namedtuple('Column', 
                                'name type_code type_name length attributes '
                                'default comment charset')

class Column(_Column):
    def __str__(self):
        components = []
        components.append("`%s`" % self.name.replace('`', '``'))
        components.append(self.type_name)
        if self.default:
            components.append('DEFAULT %s' % self.default)
        if self.comment:
            components.append("COMMENT '%s'" % self.comment.replace("'", "\\'"))

        return ' '.join(components)


#: MySQLVersion as a named tuple
class MySQLVersion(collections.namedtuple('MySQLVersion',
                                          'major minor release')):

    @classmethod
    def from_version_id(cls, value):
        """Create a MySQLVersion instance from a MYSQL_VERSION_ID int
        
        """
        return cls(major=value // 10000,
                   minor=value % 1000 // 100,
                   release=value % 100)

    def __str__(self):
        if self == (0, 0, 0):
            return "< 5.0"
        else:
            return '.'.join(map(str, self))

### Column handling
def unpack_column_attributes(*args, **kwargs):
    return ()

def unpack_column_names(names):
    return tuple(name.decode('utf8') for name in names[1:-2].split(b'\xff'))

def unpack_column_labels(labels):
    """
    Unpack a list of labels

    Returns a tuple of tuples
    """
    return tuple(
        tuple(name.decode('utf8') for name in group[1:-1].split(b'\xff'))
        for group in labels[:-1].split(b'\x00')
    )

def unpack_columns(packed_columns, table):
    names   = unpack_column_names(packed_columns.names)
    labels  = unpack_column_labels(packed_columns.labels)

    metadata = util.ByteReader(packed_columns.metadata)
    defaults = util.ByteReader(packed_columns.defaults)
    comments = util.ByteReader(packed_columns.comments)

    null_map = map(ord, defaults.read((packed_columns.null_count + 1 + 7) // 8))
    context = util.Bunch(null_map=null_map, null_bit=1, table=table)

    for fieldnr, name in enumerate(names):
        context.update(name=name,
                       fieldnr=fieldnr,
                       length=metadata.uint16_at(3, os.SEEK_CUR),
                       flags=constants.FieldFlag(metadata.uint16_at(8, os.SEEK_CUR)),
                       unireg_check=constants.Utype(metadata.uint8_at(10, os.SEEK_CUR)),
                       type_code=constants.MySQLType(metadata.uint8_at(13, os.SEEK_CUR)))

        # Point context at the relevant set of labels for the current column
        # label_id start at 1 for valid labels but our python labels are std
        # zero offset. So we subtract 1 to fix the impedance mismatch. If
        # this goes negative, this is probably not an enum field
        if context.type_code.name in ('ENUM', 'SET'):
            label_id = metadata.uint8_at(12, os.SEEK_CUR) - 1
            context.update(labels=labels[label_id])
        else:
            # clear out any previous value to aid debugging
            context.update(labels=None)

        defaults_offset = metadata.uint24_at(5, os.SEEK_CUR) - 1
        comment_length = metadata.uint16_at(15, os.SEEK_CUR)

        if context.type_code.name != 'GEOMETRY':
            charset_id = (metadata.uint8_at(11, os.SEEK_CUR) << 8) + \
                         metadata.uint8_at(14, os.SEEK_CUR)
            subtype_code = 0
        else:
            charset_id = 63 # charset 'binary'
            subtype_code = metadata.uint8_at(14, os.SEEK_CUR)
            subtype_code = constants.GeometryType(subtype_code)
        metadata.skip(17)

        charset = charsets.lookup(charset_id)
        context.update(subtype_code=subtype_code, charset=charset)

        with defaults.offset(defaults_offset):
            default = mysqltypes.unpack_default(defaults, context)
        comment = comments.read(comment_length)
        attributes = unpack_column_attributes(context)

        yield Column(name=name,
                     length=context.length,
                     type_code=context.type_code,
                     type_name=mysqltypes.format_type(context),
                     default=default,
                     attributes=attributes,
                     charset=charset,
                     comment=comment)

def parse(path):
    with open(path, 'rb') as fileobj:
        data = util.ByteReader(fileobj.read())
 

    if data.read(2) != b'\xfe\x01':
        raise IOError(errno.EINVAL, "'%s' isn't a binary .frm file" % path)

    mysql_version = MySQLVersion.from_version_id(data.uint32_at(0x0033))

    # keyinfo section
    keyinfo_offset = data.uint16_at(0x0006)
    keyinfo_length = data.uint16_at(0x000e)
    if keyinfo_length == 0xffff:
        keyinfo_length = data.uint32_at(0x002f)

    # column defualts section
    defaults_offset = keyinfo_offset + keyinfo_length
    defaults_length = data.uint16_at(0x0010)
    defaults = data.read_at(defaults_length, defaults_offset)

    # table extra / attributes section
    extrainfo_offset = defaults_offset + defaults_length
    extrainfo_length = data.uint32_at(0x0037)

    # column info section offset / lengths
    names_length = data.uint16_at(0x0004)
    header_size = 64
    forminfo_offset = data.uint32_at(header_size + names_length)
    forminfo_length = 288
    
    # "screens" section immediately follows forminfo and
    # we wish to skip it
    screens_length = data.uint16_at(forminfo_offset + 260)

    
    # Column
    null_fields = data.uint16_at(forminfo_offset + 282)
    column_count = data.uint16_at(forminfo_offset + 258)
    names_length = data.uint16_at(forminfo_offset + 268)
    labels_length = data.uint16_at(forminfo_offset + 274)
    comments_length = data.uint16_at(forminfo_offset + 284)
    metadata_offset = forminfo_offset + forminfo_length + screens_length
    metadata_length = 17*column_count # 17 bytes of metadata per column

    with data.offset(metadata_offset):
        column_data = PackedColumnData(
            count=column_count,
            null_count=null_fields,
            metadata=data.read(metadata_length),
            names=data.read(names_length),
            labels=data.read(labels_length),
            comments=data.read(comments_length),
            defaults=data.read_at(defaults_length, defaults_offset)
        )

    packed_frm_data = PackedFrmData(mysql_version=None,
                                    path=path,
                                    keyinfo=data.read_at(keyinfo_length,
                                                         keyinfo_offset),
                                    defaults=data.read_at(defaults_length,
                                                          defaults_offset),
                                    extrainfo=util.ByteReader(data.read_at(extrainfo_length,
                                                                           extrainfo_offset)),
                                    columns=column_data)


    table = Table.from_data(data, context=packed_frm_data)

    columns = list(unpack_columns(packed_frm_data.columns, table))

    indexes = list(keys.unpack_keys(packed_frm_data.keyinfo, columns, packed_frm_data.extrainfo))

    # short table comments are stored in forminfo
    table_comment_length = data.uint8_at(forminfo_offset + 46)
    if table_comment_length != 0xff:
        table_comment = data.read_at(table_comment_length,
                                     offset=forminfo_offset + 47)
    else:
        table_comment = packed_frm_data.extrainfo.bytes_prefix16()
    if table_comment:
        table_comment = table_comment.decode(table.charset.name)
        table = table._replace(options=table.options._replace(comment=table_comment))
    table = table._replace(columns=columns,
                           keys=indexes)
    return table