from copy import deepcopy

import pytest

from biography.classification import CLASSIFIER_VERSION, classify_event


def test_application_resources_are_technical_only_with_exclusive_package_evidence():
    original={'source_id':'source','title':'icon','category':'media','metadata':{}}
    package={'original_path':'history.zip!attachments/example.apk!res/icon.png'}
    result=classify_event(original,[package])
    assert result['category']=='application_resource'
    assert result['display_class']=='technical'
    assert original['category']=='media'
    standalone={'original_path':'history.zip!messages/photos/icon.png'}
    assert classify_event(original,[package,standalone])['display_class']=='media'
    assert classify_event(original,[standalone])['display_class']=='media'
    assert classify_event(original,[{'original_path':'history.zip!attachments/example.apk'}])['display_class']=='media'


def event(metadata=None, **overrides):
    return {"id": "event-one", "source_id": "source-one", "source_name": "Google", "category": "record", "title": "Export row", "body": "Retained source row", "occurred_at": None, "end_at": None, "time_precision": "unknown", "metadata": metadata or {}, **overrides}


def evidence(folder="Heart Rate", *, locator="csv:row:2", object_hash="a" * 64):
    return [{"file_id": "file-one", "original_path": f"Takeout/Fitbit/{folder}/samples.csv", "object_hash": object_hash, "locator": locator, "parser": "csv.records"}]


def test_observed_heart_rate_schema_is_a_sample_not_a_completed_activity():
    original = event({"timestamp": "2024-01-02T12:00:00Z", "beats per minute": "72", "data source": "sensor-stream-a"})
    provenance = evidence()
    saved_event, saved_provenance = deepcopy(original), deepcopy(provenance)
    result = classify_event(original, provenance)
    assert CLASSIFIER_VERSION >= 1
    assert result["source_label"] == "Fitbit"
    assert result["display_class"] == "measurement"
    assert result["category"] == "health_sample"
    assert result["is_sample"] is result["is_raw_sample"] is True
    assert result["numeric_value"] == 72
    assert result["metric"]["name"] == "heart_rate"
    assert result["unit"] == "bpm"
    assert result["metric"]["aggregation"] == "none"
    assert result["device"] == "data source: sensor-stream-a"
    assert result["device_basis"] == "recorded_data_source"
    assert result["occurred_at"] == "2024-01-02T12:00:00Z"
    assert original == saved_event and provenance == saved_provenance


def test_observed_google_health_and_fit_export_product_label_is_recognized():
    original = event({"timestamp": "2024-01-02T12:00:00Z", "beats per minute": "72"})
    provenance = [{"original_path": "Takeout/Google Health & Fit/Heart Rate/measurements.csv", "parser": "csv.records"}]
    result = classify_event(original, provenance)
    assert result["source_label"] == "Google Health & Fit"
    assert result["is_sample"] is True
    assert result["metric"]["name"] == "heart_rate"
    relative = classify_event(event({"timestamp": "2024-01-02T12:00:00Z", "beats per minute": "72"}, source_name="Google Health & Fit"), [{"original_path": "Heart Rate/samples.csv"}])
    assert relative["is_sample"] is True


def test_chatgpt_is_relabelled_only_with_parser_or_structural_proof():
    result = classify_event(event(source_name="Unknown", category="message"), [{"parser": "chatgpt.messages"}])
    assert result["source_label"] == "ChatGPT"
    assert result["display_class"] == "conversation"
    metadata = {"conversation_id": "conversation", "role": "user", "content_type": "text", "message_metadata": {}}
    assert classify_event(event(metadata, source_name="Unknown"))["source_label"] == "ChatGPT"
    assert classify_event(event(source_name="Unknown", title="A ChatGPT export"))["source_label"] == "Unknown"


@pytest.mark.parametrize("folder,metadata", [
    ("Oxygen Saturation (SpO2)", {"timestamp": "2024-01-02T12:00:00Z", "value": "97"}),
    ("Distance", {"timestamp": "2024-01-02T12:00:00Z", "distance": "123"}),
    ("Calories", {"timestamp": "2024-01-02T12:00:00Z", "calories": "15"}),
])
def test_ambiguous_units_remain_unquantified_samples(folder, metadata):
    result = classify_event(event(metadata), evidence(folder))
    assert result["is_sample"] is True
    assert result["metric"] is None
    assert result["numeric_value"] is None
    assert result["unit"] is None


def test_timestamped_scalar_health_export_is_sample_without_invented_units():
    result = classify_event(event({"dateTime": "01/02/24 12:00:00", "value": "12"}), [{"original_path": "Takeout/Google Health & Fit/Global Export Data/measurement-2024-01-02.json"}])
    assert result["display_class"] == "measurement" and result["is_sample"] is True
    assert result["metric"] is None and result["occurred_at"] is None


def test_unquantified_samples_keep_distinct_source_stream_families():
    first = classify_event(event({"timestamp": "2024-01-02T12:00:00Z", "calories": "12"}), evidence("Calories"))
    second = classify_event(event({"timestamp": "2024-01-02T12:00:00Z", "value": "97"}), evidence("Oxygen Saturation (SpO2)"))
    assert first["metric"] is second["metric"] is None
    assert first["sample_family"] == "calories"
    assert second["sample_family"] == "oxygen_saturation_spo2"
    assert first["sample_group_key"] != second["sample_group_key"]
    assert first["dedup_key"] != first["sample_group_key"]


def test_explicit_units_are_preserved_and_not_implicitly_converted():
    grams = classify_event(event({"timestamp": "2024-01-02T12:00:00Z", "weight grams": "65000", "device_id": "device-a"}), evidence("Weight"))
    kilograms = classify_event(event({"metric": {"name": "weight", "value": "65", "unit": "kg"}, "device_id": "device-a"}), evidence("Weight"))
    assert grams["numeric_value"] == 65000 and grams["unit"] == "g"
    assert kilograms["numeric_value"] == 65 and kilograms["unit"] == "kg"
    assert grams["metric"]["group_key"] != kilograms["metric"]["group_key"]


def test_device_and_account_boundaries_are_part_of_metric_grouping():
    base = {"metric": {"name": "steps", "value": "12", "unit": "steps"}, "device_id": "device-a"}
    first = classify_event(event(base), evidence("Steps"))
    other_device = classify_event(event({**base, "device_id": "device-b"}), evidence("Steps"))
    other_account = classify_event(event(base, source_id="another-account-source"), evidence("Steps"))
    keys = [result["metric"]["group_key"] for result in (first, other_device, other_account)]
    assert len(set(keys)) == 3


def test_cumulative_counter_overrides_sum_and_no_timestamp_only_sum():
    cumulative = {"steps": "1000", "aggregation": "sum", "is_cumulative": True, "start_time": "2024-01-02T12:00:00Z", "end_time": "2024-01-02T12:01:00Z"}
    assert classify_event(event(cumulative), evidence("Steps"))["metric"]["aggregation"] == "latest"
    ordinary = classify_event(event({"timestamp": "2024-01-02T12:00:00Z", "steps": "5"}), evidence("Steps"))
    assert ordinary["metric"]["aggregation"] == "none"


@pytest.mark.parametrize("flag", ["TRUE", "True", " true ", "1", 1])
def test_csv_cumulative_flags_cannot_accidentally_enable_summing(flag):
    metadata = {"steps": "1000", "aggregation": "sum", "is_cumulative": flag, "start_time": "2024-01-02T12:00:00Z", "end_time": "2024-01-02T12:01:00Z"}
    assert classify_event(event(metadata), evidence("Steps"))["metric"]["aggregation"] == "latest"
    metadata["is_cumulative"] = "unknown"
    assert classify_event(event(metadata), evidence("Steps"))["metric"]["aggregation"] == "none"


def test_sum_requires_explicit_additive_semantics_and_valid_aware_interval():
    metadata = {"metric": {"name": "steps", "value": 12, "unit": "steps", "aggregation": "sum"}, "start_time": "2024-01-02T12:00:00Z", "end_time": "2024-01-02T12:01:00Z"}
    assert classify_event(event(metadata))["metric"]["aggregation"] == "sum"
    for invalid in ({"end_time": None}, {"start_time": "2024-01-02T12:02:00Z"}, {"start_time": "2024-01-02T12:00:00", "end_time": "2024-01-02T12:01:00"}):
        assert classify_event(event({**metadata, **invalid}))["metric"]["aggregation"] == "none"
    contradictory = {**metadata, "is_cumulative": True, "metric": {**metadata["metric"], "is_cumulative": False}}
    assert classify_event(event(contradictory))["metric"]["aggregation"] == "latest"


def test_same_timestamp_value_alone_never_deduplicates_legitimate_repeats():
    sample = event({"timestamp": "2024-01-02T12:00:00Z", "beats per minute": "72"})
    assert classify_event(sample)["dedup_key"] is None
    assert classify_event({**sample, "id": "second-event"})["dedup_key"] is None
    assert classify_event(sample, evidence(locator="csv:row:2"))["dedup_key"] != classify_event(sample, evidence(locator="csv:row:3"))["dedup_key"]


def test_exact_canonical_record_proof_groups_copies_but_preserves_variants():
    sample = event({"timestamp": "2024-01-02T12:00:00Z", "beats per minute": "72"})
    first = classify_event(sample, evidence())
    copied = classify_event({**sample, "id": "another-event", "title": "A renamed export"}, evidence())
    assert first["dedup_key"] == copied["dedup_key"]
    variant = classify_event(event({"timestamp": "2024-01-02T12:00:00Z", "beats per minute": "73"}), evidence())
    assert first["occurrence_key"] == variant["occurrence_key"]
    assert first["variant_key"] != variant["variant_key"]
    assert first["dedup_key"] != variant["dedup_key"]


def test_native_identity_is_scoped_and_conflicting_payloads_remain_variants():
    first = classify_event(event({"value": 1}, native_id="native-one"))
    second = classify_event(event({"value": 2}, native_id="native-one"))
    other_source = classify_event(event({"value": 1}, native_id="native-one", source_id="source-two"))
    assert first["occurrence_key"] == second["occurrence_key"]
    assert first["dedup_key"] != second["dedup_key"]
    assert first["occurrence_key"] != other_source["occurrence_key"]
    assert classify_event(event(native_id="native-one", source_id=None))["dedup_key"] is None
    assert classify_event(event(native_id=0))["dedup_key"] is not None


def test_dates_remain_source_local_or_unknown_without_guessed_year_timezone():
    local = classify_event(event({"timestamp": "2024-03-10T02:15:00", "beats per minute": "72"}), evidence())
    assert local["occurred_at"] == "2024-03-10"
    assert local["time_precision"] == "day"
    assert local["date_basis"] == "source_local_calendar_date"
    for value in ("03/10/24 02:15:00", "02:15:00Z"):
        result = classify_event(event({"dateTime": value, "value": "5"}), evidence("Global Export Data"))
        assert result["occurred_at"] is None
        assert result["date_basis"] == "unknown"
    day = classify_event(event({"date": "2024-03-10"}))
    assert (day["occurred_at"], day["time_precision"], day["date_basis"]) == ("2024-03-10", "day", "source_calendar_date")
    assert classify_event(event(), [{"mtime": "2024-01-01T00:00:00Z"}])["occurred_at"] is None


def test_multi_metric_google_fit_rows_do_not_pick_an_arbitrary_primary_metric():
    metadata = {"Start time": "12:00:00.000Z", "End time": "12:15:00.000Z", "Step count": "10", "Calories (kcal)": "2.5", "Average heart rate (bpm)": "70"}
    provenance = [{"original_path": "Takeout/Fit/Daily activity metrics/2024-01-02.csv"}]
    result = classify_event(event(metadata), provenance)
    assert result["source_label"] == "Google Fit"
    assert len(result["metrics"]) == 3
    assert result["metric"] is result["numeric_value"] is result["unit"] is None
    assert {metric["aggregation"] for metric in result["metrics"]} == {"none"}
    assert result["occurred_at"] is None


def test_empty_google_fit_interval_is_placeholder_and_zero_is_not_missing():
    provenance = [{"original_path": "Takeout/Fit/Daily activity metrics/2024-01-02.csv"}]
    blank = classify_event(event({"Start time": "12:00:00Z", "Step count": "", "Calories (kcal)": ""}), provenance)
    assert blank["display_class"] == "technical"
    assert blank["is_sample"] is False
    zero = classify_event(event({"Start time": "12:00:00Z", "Step count": "0", "Calories (kcal)": ""}), provenance)
    assert zero["is_sample"] is True
    assert zero["numeric_value"] == 0


@pytest.mark.parametrize("value", [None, "", "NaN", "Infinity", float("nan"), float("inf"), True, "12,000", "abc"])
def test_invalid_or_absent_numeric_values_do_not_become_metrics(value):
    result = classify_event(event({"metric": {"name": "weight", "value": value, "unit": "kg"}}))
    assert result["metrics"] == []
    assert result["numeric_value"] is None


def test_unit_case_distinguishes_conductance_from_microseconds():
    conductance = classify_event(event({"metric": {"name": "sample", "value": 2, "unit": "µS"}}))
    time = classify_event(event({"metric": {"name": "sample", "value": 2, "unit": "µs"}}))
    assert conductance["unit"] == "µS" and time["unit"] == "µs"
    assert conductance["metric"]["group_key"] != time["metric"]["group_key"]


def test_known_activity_and_project_context_never_become_raw_health_samples():
    activity = classify_event(event({"beats per minute": 120}, category="activity"), evidence())
    assert activity["display_class"] == "activity" and activity["is_sample"] is False
    context = classify_event(event(category="project_context"))
    assert context["display_class"] == "technical" and context["is_sample"] is False
    mislabelled = classify_event(event({"personal_attribution": False}, category="git_commit"))
    assert mislabelled["category"] == "project_context" and mislabelled["display_class"] == "technical"
    profile = classify_event(event({"timezone": "Example/Zone", "setting_value": "enabled"}), evidence("Health Fitness Data_GoogleData"))
    assert profile["display_class"] == "document" and profile["is_sample"] is False
