import json

import pytest

from biography.classification import classify_event
from biography.parsers import parse_file


def parse(tmp_path, record, name='sleep-2024-02-01.json', source='Google', folder='Takeout/Google Health & Fit/Global Export Data'):
    path = tmp_path / 'canonical'
    path.write_text(json.dumps([record]))
    return parse_file(path, folder + '/' + name, source)


def sleep_record(**changes):
    return {'logId': 123, 'dateOfSleep': '2024-02-02',
        'startTime': '2024-02-01T23:15:00.000', 'endTime': '2024-02-02T07:15:00.000',
        'minutesAsleep': 440, 'minutesAwake': 40, 'levels': {'data': []}, **changes}


def badge_record(**changes):
    return {'encodedId': 'synthetic-badge', 'badgeType': 'DAILY_STEPS',
        'dateTime': '2024-02-01', 'timesAchieved': 12, 'name': 'Fictional step badge', **changes}


def test_sleep_preserves_local_start_end_and_separate_source_sleep_date(tmp_path):
    result = parse(tmp_path, sleep_record())
    event = result.events[0]
    assert result.parser == 'health.sleep'
    assert event['occurred_at'] == '2024-02-01'
    assert event['end_at'] == '2024-02-02'
    assert event['time_precision'] == 'day'
    assert event['metadata']['original_datetime'] == '2024-02-01T23:15:00'
    assert event['metadata']['end_date_metadata']['original_datetime'] == '2024-02-02T07:15:00'
    assert event['metadata']['dateOfSleep'] == '2024-02-02'
    assert event['metadata']['timezone_unknown'] is True
    assert event['source_native_id'] == 'sleep:123'
    assert classify_event(event)['display_class'] == 'activity'


def test_sleep_with_explicit_offsets_preserves_real_instants(tmp_path):
    event = parse(tmp_path, sleep_record(startTime='2024-02-01T23:15:00-05:00',
        endTime='2024-02-02T07:15:00-05:00')).events[0]
    assert event['occurred_at'] == '2024-02-02T04:15:00Z'
    assert event['end_at'] == '2024-02-02T12:15:00Z'
    assert event['time_precision'] == 'second'
    assert not event['metadata'].get('timezone_unknown')


@pytest.mark.parametrize('stamp',['01/02/24 23:00:00','01/02/2024 23:00:00','2024-02-30T23:00:00','',None])
def test_bad_or_ambiguous_sleep_dates_do_not_borrow_the_filename_year(tmp_path,stamp):
    event = parse(tmp_path,sleep_record(startTime=stamp,endTime=stamp)).events[0]
    assert event['occurred_at'] is None
    assert event['end_at'] is None
    assert event['metadata']['startTime'] == stamp


def test_badge_uses_recorded_day_without_expanding_achievement_count(tmp_path):
    result = parse(tmp_path,badge_record(),name='badge.json')
    assert result.parser == 'health.badges'
    assert len(result.events) == 1
    event = result.events[0]
    assert event['occurred_at'] == '2024-02-01T00:00:00Z'
    assert event['time_precision'] == 'day'
    assert event['metadata']['date_is_day_bucket'] is True
    assert event['metadata']['timesAchieved'] == 12
    assert event['source_native_id'] == 'badge:synthetic-badge'
    assert classify_event(event)['display_class'] == 'activity'


def test_health_shapes_require_export_context_and_leave_generic_short_dates_unknown(tmp_path):
    ordinary = parse(tmp_path,sleep_record(),source='Other',folder='documents')
    assert ordinary.parser == 'json.records'
    assert ordinary.events[0]['occurred_at'] is None
    short = parse(tmp_path,{'dateTime':'01/02/24 12:00:00','value':70},name='heart_rate-2024-01-02.json')
    assert short.events[0]['occurred_at'] is None
    assert short.events[0]['metadata']['dateTime'] == '01/02/24 12:00:00'


@pytest.mark.parametrize('identifier',[None,'',False,[],{}])
def test_invalid_native_ids_fall_back_to_source_occurrences(tmp_path,identifier):
    assert parse(tmp_path,sleep_record(logId=identifier)).events[0]['source_native_id'] is None
    assert parse(tmp_path,badge_record(encodedId=identifier),name='badge.json').events[0]['source_native_id'] is None
