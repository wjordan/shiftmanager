#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Redshift tests

Test Runner: PyTest
"""

from contextlib import contextmanager
import collections
import datetime
import gzip
import json
import os
import shutil
import tempfile

from mock import MagicMock, PropertyMock, ANY
import pytest
import sqlalchemy as sa
import psycopg2

import shiftmanager.redshift as rs


def cleaned(statement):
    text = str(statement)
    stripped_lines = [line.strip() for line in text.split('\n')]
    joined = '\n'.join([line for line in stripped_lines if line])
    return joined


class SqlTextMatcher(object):

    def __init__(self, text):
        self.text = text

    def __eq__(self, text):
        print(cleaned(self.text))
        print(cleaned(text))
        return cleaned(self.text) == cleaned(text)


@pytest.fixture
def mock_connection():
    mock_connection = PropertyMock()
    mock_connection.return_value = mock_connection
    mock_connection.__enter__ = MagicMock()
    mock_connection.__exit__ = MagicMock()
    return mock_connection


@pytest.fixture
def mock_s3():
    """Mock the S3 Connection, Bucket, and Key"""

    class MockBucket(object):

        s3keys = {}
        name = "com.simple.mock"

        def new_key(self, keypath):
            key_mock = MagicMock()
            self.s3keys[keypath] = key_mock
            return key_mock

        def delete_keys(self, keys):
            self.recently_deleted_keys = keys

        def reset(self):
            self.s3keys = {}
            self.recently_deleted_keys = []

    mock_S3 = MagicMock()
    mock_S3.get_bucket.return_value = MockBucket()
    return mock_S3


@pytest.fixture
def json_data():
    data = [{"a": 1}, {"a": 2}, {"a": 3}, {"a": 4},
            {"a": 5}, {"a": 6}, {"a": 7}, {"a": 8},
            {"a": 9}, {"a": 10}, {"a": 11}, {"a": 12},
            {"a": 13}, {"a": 14}, {"a": 15}, {"a": 16}]
    return data


def mogrify(self, batch, parameters=None, execute=False):
    if isinstance(parameters, collections.Mapping):
        parameters = dict([
            (key, psycopg2.extensions.adapt(val).getquoted().decode('utf-8'))
            for key, val in parameters.items()])
    elif isinstance(parameters, collections.Sequence):
        parameters = [
            psycopg2.extensions.adapt(val).getquoted()
            for val in parameters]
    if parameters:
        return batch % parameters
    return batch


@pytest.fixture
def shift(monkeypatch, mock_connection, mock_s3):
    """Patch psycopg2 with connection mocks, return conn"""

    monkeypatch.setattr('shiftmanager.Redshift.connection', mock_connection)
    monkeypatch.setattr('shiftmanager.Redshift.get_s3_connection',
                        lambda *args, **kwargs: mock_s3)
    monkeypatch.setattr('shiftmanager.Redshift.mogrify', mogrify)
    monkeypatch.setattr('shiftmanager.Redshift.execute', MagicMock())
    shift = rs.Redshift("", "", "", "",
                        aws_access_key_id="access_key",
                        aws_secret_access_key="secret_key")
    return shift


@contextmanager
def temp_test_directory():
    try:
        directory = tempfile.mkdtemp()
        yield directory

    finally:
        shutil.rmtree(directory)


def assert_execute(shift, expected):
    """Helper for asserting an executed SQL statement on mock connection"""
    assert shift.execute.called
    shift.execute.assert_called_with(SqlTextMatcher(expected))


def test_random_password(shift):
    for password in [shift.random_password() for i in range(0, 6, 1)]:
        assert len(password) < 65
        assert len(password) > 7
        for char in r'''\/'"@ ''':
            assert char not in password


def test_jsonpaths(shift):

    test_dict_1 = {"one": 1, "two": {"three": 3}}
    expected_1 = {"jsonpaths": ["$['one']", "$['two']['three']"]}
    assert expected_1 == shift.gen_jsonpaths(test_dict_1)

    test_dict_2 = {"one": [0, 1, 2], "a": {"b": [0]}}
    expected_2 = {"jsonpaths": ["$['a']['b'][1]", "$['one'][1]"]}
    assert expected_2 == shift.gen_jsonpaths(test_dict_2, 1)


def chunk_checker(file_paths):
    """Ensure that we wrote and can read all 16 integers"""
    expected_numbers = list(range(1, 17, 1))
    result_numbers = []
    for filepath in file_paths:
        with gzip.open(filepath, 'rb') as f:
            decoded = f.read().decode("utf-8")
            res = [json.loads(x)["a"] for x in decoded.split("\n")
                   if x != ""]
            result_numbers.extend(res)

    assert expected_numbers == result_numbers


def test_chunk_json_slices(shift, json_data):

    data = json_data
    with temp_test_directory() as dpath:
        for slices in range(1, 19, 1):
            with shift.chunked_json_slices(data, slices, dpath) \
                    as (stamp, paths):

                assert len(paths) == slices
                chunk_checker(paths)

            with shift.chunked_json_slices(data, slices, dpath) \
                    as (stamp, paths):

                assert len(paths) == slices
                chunk_checker(paths)


def test_create_user(shift):

    batch = shift.create_user("swiper", "swiperpass",
                              groups=['analyticsusers'],
                              wlm_query_slot_count=2)
    expected = (
        "CREATE USER swiper IN GROUP analyticsusers PASSWORD 'swiperpass';\n"
        "ALTER USER swiper SET wlm_query_slot_count = 2"
    )
    assert(batch == expected)

    batch = shift.create_user("swiper", "swiperpass",
                              valid_until=datetime.datetime(2015, 1, 1))
    expected = ("CREATE USER swiper PASSWORD 'swiperpass' "
                "VALID UNTIL '2015-01-01 00:00:00'")
    assert(batch == expected)


def test_alter_user(shift):

    statement = shift.alter_user("swiper", password="swiperpass")
    expected = "ALTER USER swiper PASSWORD 'swiperpass'"
    assert(statement == expected)


def test_dedupe(shift):

    table = sa.Table("test", sa.MetaData(),
                     sa.schema.Column("col1", sa.INTEGER))
    statement = shift.deep_copy(table, distinct=True, copy_privileges=False)

    expected = """
    LOCK TABLE test;
    ALTER TABLE test RENAME TO test$outgoing;
    CREATE TABLE test (
    col1 INTEGER
    )
    ;
    INSERT INTO test SELECT DISTINCT * from test$outgoing;
    DROP TABLE test$outgoing
    """
    assert(cleaned(statement) == cleaned(expected))


def check_key_calls(s3keys, slices):
    """Helper for checking keys have been called correctly"""

    # Ensure we wrote the correct number of files, with the correct extensions
    assert len(s3keys) == (slices + 2)
    extensions = slices*["gz"]
    extensions.extend(["manifest", "jsonpaths"])
    extensions.sort()

    res_ext = [v.split(".")[-1] for v in s3keys.keys()]
    res_ext.sort()

    assert res_ext == extensions

    # Ensure each had contents set from file once, closed once
    for val in s3keys.values():
        val.set_contents_from_file.assert_called_once_with(ANY)
        val.close.assert_called_once_with()


def get_manifest_and_jsonpaths_keys(s3keys):
    manifest = ["s3://com.simple.mock/{}".format(x)
                for x in s3keys.keys() if "manifest" in x][0]
    jsonpaths = ["s3://com.simple.mock/{}".format(x)
                 for x in s3keys.keys() if "jsonpaths" in x][0]
    return manifest, jsonpaths


def test_copy_to_json(shift, json_data):

    shift.s3conn = None

    jsonpaths = shift.gen_jsonpaths(json_data[0])

    # With cleanup
    shift.copy_json_to_table("com.simple.mock",
                             "tmp/tests/",
                             json_data,
                             jsonpaths,
                             "foo_table",
                             slices=5)

    # Get our mock bucket
    bukkit = shift.s3conn.get_bucket("foo")
    # 5 slices, one manifest, one jsonpaths
    check_key_calls(bukkit.s3keys, 5)
    mfest, jpaths = get_manifest_and_jsonpaths_keys(bukkit.s3keys)

    expect_creds = "aws_access_key_id={};aws_secret_access_key={}".format(
        "access_key", "secret_key")
    expected = """
            COPY foo_table
            FROM '{manifest}'
            CREDENTIALS '{creds}'
            JSON '{jsonpaths}'
            MANIFEST GZIP TIMEFORMAT 'auto'
            """.format(manifest=mfest, creds=expect_creds,
                       jsonpaths=jpaths)

    assert_execute(shift, expected)

    # Did we clean up?
    assert set(bukkit.recently_deleted_keys) == set(bukkit.s3keys.keys())

    # Without cleanup
    bukkit.reset()
    shift.copy_json_to_table("com.simple.mock",
                             "tmp/tests/",
                             json_data,
                             jsonpaths,
                             "foo_table",
                             slices=4,
                             clean_up_s3=False)

    bukkit = shift.s3conn.get_bucket("foo")
    # 4 slices
    check_key_calls(bukkit.s3keys, 4)

    # Should not have cleaned up S3
    assert bukkit.recently_deleted_keys == []

    # Do not cleanup local
    bukkit.reset()
    with temp_test_directory() as dpath:
        shift.copy_json_to_table("com.simple.mock",
                                 "tmp/tests/",
                                 json_data,
                                 jsonpaths,
                                 "foo_table",
                                 slices=10,
                                 local_path=dpath,
                                 clean_up_local=False)
        bukkit = shift.s3conn.get_bucket("foo")
        check_key_calls(bukkit.s3keys, 10)
        assert len(os.listdir(dpath)) == 10
