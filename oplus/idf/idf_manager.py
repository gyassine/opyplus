from contextlib import contextmanager
import io

from oplus import CONF
from oplus.idd.idd import Idd
from oplus.util import get_copyright_comment, get_string_buffer
from .cache import Cached, cached, clear_cache
from .style import IdfStyle, style_library
from .record_manager import RecordManager
from .exceptions import BrokenIdfError, IsPointedError
from .queryset import QuerySet


class IdfManager(Cached):
    record_manager_cls = RecordManager  # for subclassing

    # ----------------------------------------------- INITIALIZE -------------------------------------------------------
    def __init__(self, idf, path_or_content, idd_or_path=None, encoding=None, style=None):
        self.activate_cache()
        self._idf = idf
        self._idd = Idd.get_idd(idd_or_path, encoding=encoding)
        self._encoding = CONF.encoding if encoding is None else encoding
        self._constructing_mode = False

        # get string buffer and store path (for info)
        buffer, path = get_string_buffer(path_or_content, "idf", self._encoding)
        self._path = path_or_content

        # raw parse and parse
        with buffer as f:
            self._records, self._head_comments = self.parse(f, style)

    # ----------------------------------------------- EXPOSE -----------------------------------------------------------
    @staticmethod
    def copyright_comment():
        return get_copyright_comment()

    @property
    def idd(self):
        return self._idd

    @property
    def idf(self):
        return self._idf

    # --------------------------------------------- CONSTRUCT ----------------------------------------------------------
    @property
    @contextmanager
    def under_construction(self):
        """
        Allows the user to deactivate new reference checks while adding records. The whole idf is checked afterwards.
        This allows to construct idfs more efficiently.
        """
        self._constructing_mode = True
        yield
        self.check_duplicate_references()
        self._constructing_mode = False

    def parse(self, file_like, style=None):
        """
        Records are created from string. They are not attached to idf manager yet.
        in idf: header comment, chapter comments, records
        in record: head comment, field comments, tail comment
        """
        if style is None:
            style = style_library[CONF.default_read_style]
        if isinstance(style, IdfStyle):
            style = style
        elif isinstance(style, str):
            if style in style_library.keys():
                style = style_library[style]
            else:
                style = style_library[CONF.default_read_style]
        else:
            style = style_library[CONF.default_read_style]

        records, head_comments = [], ""
        record_manager = None
        make_new_record = True

        tail_comments = ""

        for i, raw_line in enumerate(file_like):
            # GET LINE CONTENT AND COMMENT
            split_line = raw_line.split("!")

            # No "!" in the raw_line
            if len(split_line) == 1:
                # This is an empty line
                if len(split_line[0].strip()) == 0:
                    content, comment = None, None
                # This is a record line with no comments
                else:
                    content, comment = split_line[0].strip(), None
            # There is at least one "!" in the raw_line
            else:
                # This is a comment line
                if len(split_line[0].strip()) == 0:
                    content, comment = None, "!".join(split_line[1:])
                # This is a record line with a comment
                else:
                    content, comment = split_line[0].strip(), "!".join(split_line[1:])

            # SKIP CURRENT LINE IF VOID
            if (content, comment) == (None, None):
                continue

            # NO CONTENT
            if not content:
                if record_manager is None:  # head idf comment
                    if style is None:
                        head_comments += comment.strip() + "\n"
                    elif comment[:len(style.chapter_key)] == style.chapter_key:
                        continue
                    elif comment[:len(style.head_key)] == style.head_key:
                        comment = comment[len(style.head_key):].strip()
                        head_comments += comment + "\n"
                else:
                    if style is None:
                        continue
                    elif comment[:len(style.chapter_key)] == style.chapter_key:
                        continue
                    elif comment[:len(style.tail_record_key)] == style.tail_record_key:
                        comment = comment[len(style.tail_record_key):].strip().replace("\n", "")
                        if style.tail_type == "before":
                            tail_comments += comment + "\n"
                        elif style.tail_type == "after":
                            record_manager.add_tail_comment(comment)

                continue

            # CONTENT
            # check if record end and prepare
            record_end = content[-1] == ";"
            content = content[:-1]  # we tear comma or semi-colon
            content_l = [text.strip() for text in content.split(",")]

            if comment:
                if style is None:
                    comment = comment.strip().replace("\n", "")
                elif comment[:len(style.record_key)] == style.record_key:
                    comment = comment[len(style.record_key):].strip().replace("\n", "")
                else:
                    comment = None

            field_comment = comment
            # record creation if needed
            if make_new_record:
                if not record_end and len(content_l) > 1:
                    head_comment = None
                    field_comment = comment
                else:
                    head_comment = comment
                    field_comment = None

                record_manager = self.record_manager_cls(content_l[0].strip(), self, head_comment=head_comment)
                records.append(record_manager.record)
                # prepare in case fields on the same line
                content_l = content_l[1:]
                make_new_record = False

            # fields
            for value_s in content_l:
                record_manager.add_field(value_s, comment=field_comment)

            # signal that new record must be created
            if record_end:
                if style:
                    if style.tail_type == "before":
                        record_manager.add_tail_comment(tail_comments)
                        tail_comments = ""
                make_new_record = True

        return records, head_comments

    # ----------------------------------------------- LINKS ------------------------------------------------------------
    @cached
    def get_pointed_link(self, pointing_ref, pointing_index, pointing_raw_value):
        # get field descriptor
        fieldd = self._idd.get_record_descriptor(pointing_ref).get_field_descriptor(pointing_index)

        # check if object-list
        assert fieldd.detailed_type == "object-list", \
            "Only 'object-list' fields can point on an object. " \
            f"Wrong field given. Ref: '{pointing_ref}', index: '{pointing_index}'."

        # check if a record is pointed
        if pointing_raw_value == "":  # no record pointed
            return None, None

        # iter through link possibilities and return if found
        link_names_l = fieldd.get_tag("object-list")
        for link_name in link_names_l:
            for od, field_index in self._idd.pointed_links(link_name):
                for record in self.filter_by_ref(od.ref):
                    if record._.get_raw_value(field_index) == pointing_raw_value:
                        return record, field_index

        raise RuntimeError(
            f"Link not found. "
            f"Field 'object-list' tag values: {str(link_names_l)}, field value : '{pointing_raw_value}'"
        )

    @cached
    def get_pointing_links_l(self, pointed_ref, pointed_index, pointed_raw_value):
        # get field descriptor
        fieldd = self.idd.get_record_descriptor(pointed_ref).get_field_descriptor(pointed_index)

        # check if reference
        assert fieldd.detailed_type == "reference", \
            "Only 'reference' fields can be pointed by an object. Wrong field given. " \
            f"Ref: '{pointed_ref}', index: '{pointed_index}'."

        # check if a record can be pointing
        if pointed_raw_value == "":
            return []

        # fetch links
        links_l = []
        for link_name in fieldd.get_tag("reference"):
            for record_descriptor, pointing_index in self.idd.pointing_links(link_name):
                for record in self.filter_by_ref(record_descriptor.ref):
                    if pointing_index >= record._.fields_nb:
                        continue
                    if record._.get_raw_value(pointing_index) == pointed_raw_value:
                        links_l.append([record, pointing_index])
        return links_l

    def check_new_reference(self, new_record_ref, new_record_index, reference):
        if reference == "":
            return None
        # check that there is no duplicate reference (i.e. none of the links which will point to this field already
        # points to another field with the same reference)
        links_l = self.get_pointing_links_l(new_record_ref, new_record_index, reference)
        if len(links_l) != 0:
            raise BrokenIdfError(
                "New record has same reference at index '%s' as other record of same link name. "
                "Other record ref: '%s', index: '%s'. The value at that field must be changed." %
                (new_record_index, links_l[0][0]._.ref, links_l[0][1])
            )

    def check_duplicate_references(self):
        # we create a dict containing for each link_name a set of references to check that they are unique
        ref_d = dict()
        for record in self._records:
            # check reference uniqueness
            record_descriptor = self._idd.get_record_descriptor(record._.ref)
            for i in range(record._.fields_nb):
                fieldd = record_descriptor.get_field_descriptor(i)
                if fieldd.detailed_type == "reference":
                    reference = record._.get_raw_value(i)
                    for link_name in fieldd.get_tag("reference"):
                        # for each link name add the reference to the set to check for uniqueness
                        if link_name not in ref_d:
                            ref_d[link_name] = set()
                        if reference in ref_d[link_name]:
                            raise BrokenIdfError(
                                "Reference duplicate for link name: {}\n".format(link_name) +
                                "Reference: {}\n".format(reference) +
                                "Detected while checking record ref: {}\n".format(record._.ref) +
                                "Field: {}".format(i)
                            )
                        ref_d[link_name].add(reference)

    # ------------------------------------------ MANAGE RECORDS --------------------------------------------------------
    def has_record(self, record):
        return record in self._records

    @clear_cache
    def add_record(self, new_str, position=None):
        """
        From str
        """
        # create record
        records, comments_l = self.parse(io.StringIO(new_str))  # comments not used (only for global idf parse)
        assert len(records) == 1, "Wrong number of records created: %i" % len(records)
        new_record = records[0]
        return self.add_record_from_parsed(new_record._, position=position)

    @clear_cache
    def add_record_from_parsed(self, raw_parsed_record_manager, position=None):  # todo: change name and move to table
        """checks references uniqueness"""
        new_record = raw_parsed_record_manager.record  # change name since no more raw parsed

        # check reference uniqueness
        record_descriptor = self._idd.get_record_descriptor(new_record._.ref)
        for i in range(new_record._.fields_nb):
            fieldd = record_descriptor.get_field_descriptor(i)
            if fieldd.detailed_type == "reference" and not self._constructing_mode:
                self.check_new_reference(record_descriptor.ref, i, new_record._.get_raw_value(i))

        # add record
        if position is None:
            self._records.append(new_record)
        else:
            self._records.insert(position, new_record)

        # return new record
        return new_record

    @clear_cache
    def remove_record(self, record, raise_if_pointed=True):
        """
        Arguments
        ---------
        raise_if_pointed: raises Exception if is pointed by other records.
            Else, sets all pointing record fields to None.
        """
        # check if record is pointed, if asked
        pointing_links_l = record._.get_pointing_links_l()
        if raise_if_pointed and len(pointing_links_l) > 0:
            raise IsPointedError(
                "Can't remove record if other records are pointing to it and 'check' is "
                "True. Pointing records: '%s'" % [o for (o, i) in pointing_links_l]
            )

        # remove from pointing
        for pointing_record, pointing_index in pointing_links_l:
            pointing_record._.remove_values_that_point(record)

        # remove pointed
        record._.remove_values_that_point()

        # delete obsolete attributes
        record._.neutralize()

        # remove from idf
        index = self._records.index(record)
        del self._records[index]

        return index

    @cached
    def filter_by_ref(self, ref=None):
        if ref is None:
            return QuerySet(self._records)
        return QuerySet(self._records)(ref)

    # ------------------------------------------ MANAGE COMMENTS -------------------------------------------------------
    def get_comment(self):
        return self._head_comments

    @clear_cache
    def set_comment(self, value):
        self._head_comments = str(value).strip()

    # ------------------------------------------------ COMMUNICATE -----------------------------------------------------
    def info(self, sort_by_group=False, detailed=False):
        """
        Indicates all records references contained in current idf.

        Arguments
        ---------
        sort_by_group: will sort record descriptors by group
        detailed: will give all record descriptors' associated tags

        Returns
        -------
        a text describing the information on record contained in idd file
        """
        # rds: records descriptors
        def _get_rds_info(_ods, _line_start=""):
            _msg = ""
            for _rd in sorted(_ods, key=lambda x: x.ref):
                _msg += "\n%s%s" % (_line_start, _rd.ref)
                if detailed:
                    for _tag in _rd.tags:
                        _msg += "\n%s\t* %s: %s" % (_line_start, _tag, _rd.get_tag(_tag))
            return _msg

        rds_refs_set = set([record.ref for record in self._records])
        name = "Idf: '%s'" % self._path
        msg = "%s\n%s\n%s" % ("-"*len(name), name, "-"*len(name))
        if sort_by_group:
            for group_name in self._idd.groups_l:
                ods_l = []
                for od in self._idd.get_record_descriptors_by_group(group_name):
                    if od.ref in rds_refs_set:
                        ods_l.append(od)
                if len(ods_l) > 0:
                    msg += "\nGroup - %s" % group_name
                    msg += _get_rds_info(ods_l, _line_start="\t")
        else:
            msg += _get_rds_info([self._idd.get_record_descriptor(od_ref) for od_ref in rds_refs_set])

        return msg

    def to_str(self, style=None, add_copyright=True, clean=False):
        # todo: change clean to sort, make default true, (and order table refs by idd order ?)
        if style is None:
            style = style_library[CONF.default_write_style]
        if isinstance(style, IdfStyle):
            style = style
        elif isinstance(style, str):
            if style in style_library.keys():
                style = style_library[style]
            else:
                style = style_library[CONF.default_write_style]
        else:
            style = style_library[CONF.default_write_style]
        content = ""

        # idf comments
        idf_comment = self._head_comments
        if add_copyright:
            msg = self.copyright_comment()
            if msg not in idf_comment:
                idf_comment = msg + "\n" + idf_comment

        for comment in idf_comment.split("\n")[:-1]:
            content += style.get_head_comment(comment)

        if clean:
            # store records str (before order)
            records = []  # [(table_ref, record_str), ...]
            for obj in self._records:
                records.append((obj.ref, "\n%s" % obj._.to_str(style="idf", idf_style=style)))

            # iter sorted list and add chapter titles
            current_ref = None

            for (record_ref, record_str) in sorted(records):
                # write chapter title if needed
                if record_ref != current_ref:
                    current_ref = record_ref
                    content += "\n" + style.get_chapter_title(current_ref)

                # write record
                content += record_str

        else:
            for record in self._records:
                content += "\n%s" % record._.to_str(style="idf", idf_style=style)

        return content

    def save_as(self, file_or_path, style=None, add_copyright=True, clean=False):
        is_path = isinstance(file_or_path, str)
        f = open(file_or_path, "w", encoding=self._encoding) if is_path else file_or_path
        f.write(self.to_str(style=style, add_copyright=add_copyright, clean=clean))
        if is_path:
            f.close()

    def copy(self, add_copyright=True):
        content = self.to_str(add_copyright=add_copyright)
        return self.idf.__class__(content, self.idd, encoding=self._encoding)
