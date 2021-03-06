#!/usr/bin/env python

"""
Fixes a MySQL dump made with the right format so it can be directly imported to a new PostgreSQL database.

Dump using: mysqldump --compatible=postgresql --skip-triggers --default-character-set=utf8 -r databasename.mysql -u root databasename
"""
#TODO: Order output to place DDL first and all INSERTs after; Option to split into two files?

import os, re, sys, time

# Invalid MySQL dates (0000-00-00) which may also appear in timestamps
#TODO: Danger of replacing non-date strings of the same pattern?
zeroyr = re.compile(r'0000-(\d\d)-(\d\d)')
zeromm = re.compile(r'(\d\d\d\d)-00-(\d\d)')
zerodd = re.compile(r'(\d\d\d\d)-(\d\d)-00')

insrt = re.compile(r'INSERT INTO "(\w+)"')  # Split INSERT statement from VALUES clause and cover case when "VALUES" appears in data

def parse(input_filename, output_filename, rollback):
    "Feed it a file, and it'll output a fixed one"

    # State storage
    if input_filename == "-":
        num_bytes = -1
    else:
        num_bytes = os.path.getsize(input_filename)
    tables = {}
    current_table = None
    creation_lines = []
    enum_types = []
    foreign_key_lines = []
    fulltext_key_lines = []
    sequence_lines = []
    cast_lines = []
    column_comments = []
    num_inserts = 0
    started = time.time()

    # Open output file and write header. Logging file handle will be stdout
    # unless we're writing output to stdout, in which case NO PROGRESS FOR YOU.
    if output_filename == "-":
        output = sys.stdout
        logging = open(os.devnull, "w")
    else:
        output = open(output_filename, "w")
        logging = sys.stdout

    if input_filename == "-":
        input_fh = sys.stdin
    else:
        input_fh = open(input_filename)


    output.write("-- Converted by mysql2psql.py\n")
    if rollback: output.write("START TRANSACTION;\n")
    output.write("SET standard_conforming_strings=off;\n")
    output.write("SET escape_string_warning=off;\n")
    output.write("SET CLIENT_ENCODING TO 'UTF8';\n")
    output.write("SET CONSTRAINTS ALL DEFERRED;\n\n")

    bytes = 0.0
    for i, line in enumerate(input_fh):
        time_taken = time.time() - started
        bytes += len(line)  # assuming line is utf8 encoded from dump options
        percentage_done = bytes / num_bytes
        secs_left = (time_taken / percentage_done) - time_taken
        logging.write("\rLine %i (%.2f%%) [%s tables] [%s inserts] [ETA: %i min %i sec]" % (
            i + 1,
            percentage_done * 100,
            len(tables),
            num_inserts,
            secs_left // 60,
            secs_left % 60,
        ))
        logging.flush()

        #TODO: Some bytea inserts failing with UTF-8 conversion errors on 0x00 characters
        line = unicode(line, errors='replace').strip().replace(r"\\", "WUBWUBREALSLASHWUB").replace(r"\'", "''").replace("WUBWUBREALSLASHWUB", r"\\")

        # Ignore comment lines, SETs, LOCKs
        if line.startswith("--") or line.startswith("/*") or line.startswith("SET") or line.startswith("LOCK TABLES") or line.startswith("UNLOCK TABLES") or not line:
            continue

        # Outside of anything handling
        if current_table is None:
            if line.startswith("DROP TABLE"):
                name = line.split('"')[1].lower()
                line = 'DROP TABLE IF EXISTS "%s";' % name
                output.write(line.encode("utf8", 'replace') + "\n")
            # Start of a table creation statement?
            elif line.startswith("CREATE TABLE"):
                current_table = line.split('"')[1].lower()
                tables[current_table] = {"columns": []}
                creation_lines = []
            # Inserting data into a table?
            elif line.startswith("INSERT INTO"):
                null, table, values = insrt.split(line)
                values = values.strip()
                values = zeroyr.sub('0001-01-01', values)
                line = "INSERT INTO %s %s" % (table, values)
                output.write(line.encode("utf8", 'replace') + "\n")
                num_inserts += 1
            else:
                print "\n ! Unknown line in main body: %s" % line

        # Inside-create-statement handling
        else:
            # Is it a column?
            if line.startswith('"'):
                useless, name, definition = line.strip(",").split('"',2)
                name = name.lower()
                try:
                    type, extra = definition.strip().split(" ", 1)

                    # This must be a tricky enum
                    if ')' in extra:
                        type, extra = definition.strip().split(")")

                except ValueError:
                    type = definition.strip()
                    extra = ""
                extra = re.sub("CHARACTER SET [\w\d]+\s*", "", extra.replace("unsigned", ""))
                extra = re.sub("COLLATE [\w\d]+\s*", "", extra.replace("unsigned", ""))

                comment = re.search("COMMENT '.+'", extra)
                if comment:
                    column_comments.append(comment.group().replace("COMMENT ", "COMMENT ON COLUMN %s.%s IS " % (current_table, name)))
                    extra = re.sub("COMMENT '.+'", "", extra)

                # See if it needs type conversion
                final_type = None
                set_sequence = None
                if type.startswith("tinyint("):
                    type = "smallint"
                    set_sequence = True
                elif type.startswith("smallint("):
                    type = "smallint"
                    set_sequence = True
                elif type.startswith("mediumint("):
                    type = "smallint"
                    set_sequence = True
                elif type.startswith("int("):
                    type = "integer"
                    set_sequence = True
                elif type.startswith("bigint("):
                    type = "bigint"
                    set_sequence = True
                elif type == "longtext":
                    type = "text"
                elif type == "mediumtext":
                    type = "text"
                elif type == "tinytext":
                    type = "text"
                elif type.startswith("varchar("):
                    type = "text"
                elif type == "datetime":
                    type = "timestamp with time zone"
                elif type.startswith("double("):
                    type = "numeric"
                    set_sequence = True
                elif type == "double":
                    type = "double precision"
                elif type.startswith("float("):
                    type = "numeric"
                    set_sequence = True
                elif type.startswith("varbinary"):
                    type = "bytea"
                elif type.endswith("blob"):
                    type = "bytea"
                elif type.startswith("enum(") or type.startswith("set("):
                    types_str = type.split("(")[1].rstrip(")").rstrip('"')
                    types_arr = [type_str.strip('\'') for type_str in types_str.split(",")]

                    # Considered using values to make a name, but its dodgy
                    # enum_name = '_'.join(types_arr)
                    enum_name = "{0}_{1}".format(current_table, name)

                    if enum_name not in enum_types:
                        output.write("CREATE TYPE {0} AS ENUM ({1}); \n".format(enum_name, types_str));
                        enum_types.append(enum_name)

                    type = enum_name

                if final_type:
                    cast_lines.append("ALTER TABLE \"%s\" ALTER COLUMN \"%s\" DROP DEFAULT, ALTER COLUMN \"%s\" TYPE %s USING CAST(\"%s\" as %s)" % (current_table, name, name, final_type, name, final_type))
                # ID fields need sequences [if they are integers?]
                if name == "id" and set_sequence is True:
                    sequence_lines.append("DROP SEQUENCE IF EXISTS %s_id_seq" % (current_table))
                    sequence_lines.append("CREATE SEQUENCE %s_id_seq" % (current_table))
                    sequence_lines.append("SELECT setval('%s_id_seq', max(id)) FROM %s" % (current_table, current_table))
                    sequence_lines.append("ALTER TABLE \"%s\" ALTER COLUMN \"id\" SET DEFAULT nextval('%s_id_seq')" % (current_table, current_table))
                # Record it
                creation_lines.append('"%s" %s %s' % (name, type, extra))
                tables[current_table]['columns'].append((name, type, extra))
            # Is it a constraint or something?
            elif line.startswith("PRIMARY KEY"):
                creation_lines.append("PRIMARY KEY %s" % line.rstrip(",").split("PRIMARY KEY")[1].strip().lower())
            elif line.startswith("CONSTRAINT"):
                foreign_key_lines.append("ALTER TABLE \"%s\" ADD CONSTRAINT %s DEFERRABLE INITIALLY DEFERRED" % (current_table, line.split("CONSTRAINT")[1].strip().rstrip(",").lower()))
                foreign_key_lines.append("CREATE INDEX ON \"%s\" %s" % (current_table, line.split("FOREIGN KEY")[1].split("REFERENCES")[0].strip().rstrip(",").lower()))
            elif line.startswith("UNIQUE KEY"):
                creation_lines.append("UNIQUE (%s)" % line.split("(")[1].split(")")[0].lower())
            elif line.startswith("FULLTEXT KEY"):
                fulltext_keys = " || ' ' || ".join( line.split('(')[-1].split(')')[0].replace('"', '').split(',').lower() )
                fulltext_key_lines.append("CREATE INDEX ON %s USING gin(to_tsvector('english', %s))" % (current_table, fulltext_keys))
            elif line.startswith("KEY"):
                pass
            # Is it the end of the table?
            elif line == ");":
                output.write("CREATE TABLE \"%s\" (\n" % current_table)
                for i, line in enumerate(creation_lines):
                    # Replace zero date components with valid default date values of 01
                    line = zeroyr.sub(r'0001-\1-\2', line)
                    line = zeromm.sub(r'\1-01-\2', line)
                    line = zerodd.sub(r'\1-\2-01', line)
                    output.write("    %s%s\n" % (line, "," if i != (len(creation_lines) - 1) else ""))
                output.write(');\n\n')
                current_table = None
            # ???
            else:
                print "\n ! Unknown line inside table creation: %s" % line


    # Finish file
    output.write("\n-- Post-data save --\n")
    if rollback: output.write("COMMIT;\n")
    if rollback: output.write("START TRANSACTION;\n")

    # Write typecasts out
    output.write("\n-- Typecasts --\n")
    for line in cast_lines:
        output.write("%s;\n" % line)

    # Write FK constraints out
    output.write("\n-- Foreign keys --\n")
    for line in foreign_key_lines:
        output.write("%s;\n" % line)

    # Write sequences out
    output.write("\n-- Sequences --\n")
    for line in sequence_lines:
        output.write("%s;\n" % line)

    # Write column comments out
    output.write("\n-- Comments --\n")
    for line in column_comments:
        output.write("%s;\n" % line)

    # Write full-text indexkeyses out
    output.write("\n-- Full Text keys --\n")
    for line in fulltext_key_lines:
        output.write("%s;\n" % line)

    # Finish file
    output.write("\n")
    if rollback: output.write("COMMIT;\n")
    print ""


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print 'Usage: mysql2psql.py database.mysql database.psql [rollback]'
        print 'Dump using: mysqldump --compatible=postgresql --skip-triggers --default-character-set=utf8 -r database.mysql -u root databasename\n'
        sys.exit()

    rollback = False
    if len(sys.argv) > 3 and sys.argv[3] == 'rollback': rollback = True

    parse(sys.argv[1], sys.argv[2], rollback)
