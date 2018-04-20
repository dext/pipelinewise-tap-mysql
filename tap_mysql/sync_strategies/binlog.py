#!/usr/bin/env python3
# pylint: disable=duplicate-code,too-many-locals

import copy

from datetime import datetime

import pytz

import singer
from singer import metadata
from singer import utils
from singer.schema import Schema

import pymysql.connections
import tap_mysql.sync_strategies.common as common

from tap_mysql.connection import make_connection_wrapper

from pymysqlreplication import BinLogStreamReader
from pymysqlreplication.event import RotateEvent
from pymysqlreplication.row_event import (
        DeleteRowsEvent,
        UpdateRowsEvent,
        WriteRowsEvent,
    )

LOGGER = singer.get_logger()

SDC_DELETED_AT = "_sdc_deleted_at"

UPDATE_BOOKMARK_PERIOD = 1000

def add_automatic_properties(catalog_entry, columns):
    catalog_entry.schema.properties[SDC_DELETED_AT] = Schema(
        type=["null", "string"],
        format="date-time"
        )

    columns.append(SDC_DELETED_AT)

    return columns


def verify_binlog_config(connection, catalog_entry):
    with connection.cursor() as cur:
        cur.execute("""
        SELECT  @@binlog_format    AS binlog_format,
        @@binlog_row_image AS binlog_row_nimage;
        """)

        binlog_format, binlog_row_image = cur.fetchone()

        if binlog_format != 'ROW':
            raise Exception("Unable to replicate stream({}) with binlog because binlog_format is not set to 'ROW': {}."
                            .format(catalog_entry.stream, binlog_format))

        if binlog_row_image != 'FULL':
            raise Exception("Unable to replicate stream({}) with binlog because binlog_row_image is not set to 'FULL': {}."
                            .format(catalog_entry.stream, binlog_row_image))


def verify_log_file_exists(connection, catalog_entry, log_file, log_pos):
    with connection.cursor() as cur:
        cur.execute("SHOW BINARY LOGS")
        result = cur.fetchall()

        existing_log_file = list(filter(lambda log: log[0] == log_file, result))

        if not existing_log_file:
            raise Exception("Unable to replicate stream({}) with binlog because log file {} does not exist."
                            .format(catalog_entry.stream, log_file))

        current_log_pos = existing_log_file[0][1]

        if log_pos > current_log_pos:
            raise Exception("Unable to replicate stream({}) with binlog because requested position ({}) for log file {} is greater than current position ({})."
                            .format(catalog_entry.stream, log_pos, log_file, current_log_pos))

def fetch_current_log_file_and_pos(connection):
    with connection.cursor() as cur:
        cur.execute("SHOW MASTER STATUS")

        result = cur.fetchone()
        current_log_file, current_log_pos = result[0:2]

        return current_log_file, current_log_pos

def fetch_server_id(connection):
    with connection.cursor() as cur:
        cur.execute("SELECT @@server_id")
        server_id = cur.fetchone()[0]

        return server_id

def row_to_singer_record(catalog_entry, version, vals, columns, time_extracted):
    filtered_vals = {k:v for k,v in vals.items() if k in columns}
    rec_cols, rec_vals = zip(*filtered_vals.items())

    return common.row_to_singer_record(catalog_entry, version, rec_vals, rec_cols, time_extracted)


def sync_table(connection, config, catalog_entry, state, columns):
    log_file = singer.get_bookmark(state,
                                   catalog_entry.tap_stream_id,
                                   'log_file')

    log_pos = singer.get_bookmark(state,
                                  catalog_entry.tap_stream_id,
                                  'log_pos')

    verify_binlog_config(connection, catalog_entry)
    verify_log_file_exists(connection, catalog_entry, log_file, log_pos)

    stream_version = common.get_stream_version(catalog_entry.tap_stream_id, state)
    state = singer.write_bookmark(state,
                                  catalog_entry.tap_stream_id,
                                  'version',
                                  stream_version)

    yield singer.ActivateVersionMessage(
        stream=catalog_entry.stream,
        version=stream_version
    )

    server_id = fetch_server_id(connection)

    connection_wrapper = make_connection_wrapper(config)

    reader = BinLogStreamReader(
        connection_settings={},
        server_id=server_id,
        log_file=log_file,
        log_pos=log_pos,
        resume_stream=True,
        only_events=[RotateEvent, WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent],
        pymysql_wrapper=connection_wrapper
    )

    table_path = (catalog_entry.database, catalog_entry.stream)

    time_extracted = utils.now()

    LOGGER.info("Starting binlog replication with log_file=%s, log_pos=%s", log_file, log_pos)

    rows_saved = 0

    for binlog_event in reader:
        if reader.log_file == log_file and reader.log_pos == log_pos:
            LOGGER.info("Skipping event for log_file=%s and log_pos=%s as it was processed last sync",
                        reader.log_file,
                        reader.log_pos)
            continue

        if isinstance(binlog_event, RotateEvent):
            state = singer.write_bookmark(state,
                                          catalog_entry.tap_stream_id,
                                          'log_file',
                                          binlog_event.next_binlog)
            state = singer.write_bookmark(state,
                                          catalog_entry.tap_stream_id,
                                          'log_pos',
                                          binlog_event.position)

        elif (binlog_event.schema, binlog_event.table) == table_path:
            if isinstance(binlog_event, WriteRowsEvent):
                for row in binlog_event.rows:
                    yield row_to_singer_record(catalog_entry,
                                               stream_version,
                                               row['values'],
                                               columns,
                                               time_extracted)
                    state = singer.write_bookmark(state,
                                                  catalog_entry.tap_stream_id,
                                                  'log_file',
                                                  log_file)
                    rows_saved = rows_saved + 1


            elif isinstance(binlog_event, UpdateRowsEvent):
                for row in binlog_event.rows:
                    yield row_to_singer_record(catalog_entry,
                                               stream_version,
                                               row['after_values'],
                                               columns,
                                               time_extracted)
                    rows_saved = rows_saved + 1
            elif isinstance(binlog_event, DeleteRowsEvent):
                for row in binlog_event.rows:
                    event_ts = datetime.utcfromtimestamp(binlog_event.timestamp).replace(tzinfo=pytz.UTC)

                    vals = row['values']
                    vals[SDC_DELETED_AT] = event_ts

                    yield row_to_singer_record(catalog_entry,
                                               stream_version,
                                               vals,
                                               columns,
                                               time_extracted)
                    rows_saved = rows_saved + 1

            state = singer.write_bookmark(state,
                                      catalog_entry.tap_stream_id,
                                      'log_file',
                                      reader.log_file)

            state = singer.write_bookmark(state,
                                      catalog_entry.tap_stream_id,
                                      'log_pos',
                                      reader.log_pos)

            if rows_saved % UPDATE_BOOKMARK_PERIOD == 0:
                yield singer.StateMessage(value=copy.deepcopy(state))

        yield singer.StateMessage(value=copy.deepcopy(state))
