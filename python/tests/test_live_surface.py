"""The package namespace and ordinary HTTP helpers form a closed public surface."""

import types
import unittest

import httpx
import system_zero


API_CODES = set("""
  attachment_too_large audio_not_available capacity_exhausted committee_not_available
  daily_dispatch_limit deliberate_not_available document_controls_not_available document_decode_failed
  document_not_available document_pictures_not_available document_too_large document_unsupported
  https_required idempotency_key_reuse image_not_available injection_suspected
  input_quarantined insufficient_credits invalid_criteria invalid_request
  long_context_not_available long_duration_unsupported long_evidence_incomplete long_media_guard_refused
  long_no_readable_window long_options_unsupported long_phrase_invalid long_question_type_unsupported
  long_question_unsupported long_recording_not_available long_state_unsupported long_too_many_phrases
  media_decode_failed media_too_large media_unpriced media_unsupported
  payload_too_large question_not_grounded question_too_large questions_too_large
  rate_limit_exceeded request_refused service_unavailable staging_attempt_ceiling
  state_too_large state_too_large_unguarded system_zero_timeout system_zero_unavailable
  upload_in_use upload_not_found upload_not_ready video_not_available
  workspace_limit_exceeded workspace_quarantined
""".split())
GATEWAY_CODES = set("""
    auth_error account_suspended model_not_found unknown_endpoint method_not_allowed invalid_trace_header invalid_api_key invalid_idempotency_key
    idempotency_conflict idempotency_replay_unavailable idempotency_replayed_error idempotency_unavailable limiter_unavailable
    maintenance internal_error concurrency_limit_exceeded monthly_cap_reached
""".split())


EXPECTED = set("""
    SystemZero VERSION __version__ Alternative Decision LegacyDecision QuestionRefusal
    decision_for decisions_for ErrorCode KNOWN_ERROR_CODES SYSTEM_ZERO_ERROR_CODES GATEWAY_ERROR_CODES
    RateLimitInfo ResponseMetadata ResponseResult MAX_FLOW_STEPS flow_idempotency_key flow_request_hash run_flow validate_flow idempotency_key
    request_fingerprint run_batch evaluate report_json report_markdown score_refusal
    score_reply validate_expected AccuracyMetrics BatchItem BatchOptions BatchRecord BatchResult
    BatchUsage EvaluationRecord EvaluationReport ExpectedAnswers FlowAnswerTrace FlowBranchTrace
    FlowCondition FlowDefinition FlowErrorTrace FlowNext FlowOptions FlowRequest FlowRule FlowStep
    FlowStepTrace FlowTemplate FlowTrace QuestionScore QuestionType ReliabilityBin Usage
    DecideRequest DecideReply Model ModelList Question
    SystemZeroError InvalidRequestError AuthenticationError PermissionError NotFoundError RateLimitError
    OverloadedError APIError ServerError APIConnectionError APITimeoutError PayloadTooLargeError InvalidCriteriaError ServiceUnavailableError CapabilityNotAvailableError RouteNotAvailableError ImageNotAvailableError AudioNotAvailableError
    DocumentNotAvailableError VideoNotAvailableError LongContextNotAvailableError LongRecordingNotAvailableError CommitteeNotAvailableError DeliberateNotAvailableError
""".split())


class PublicSurfaceTests(unittest.TestCase):
    def test_error_conveniences_equal_the_api_catalog(self):
        self.assertEqual(len(API_CODES), 54)
        self.assertEqual(len(GATEWAY_CODES), 17)
        self.assertEqual(set(system_zero.SYSTEM_ZERO_ERROR_CODES), API_CODES)
        self.assertEqual(len(system_zero.SYSTEM_ZERO_ERROR_CODES), len(API_CODES))
        self.assertEqual(set(system_zero.GATEWAY_ERROR_CODES), GATEWAY_CODES)
        self.assertEqual(len(system_zero.GATEWAY_ERROR_CODES), len(GATEWAY_CODES))
        self.assertEqual(set(system_zero.KNOWN_ERROR_CODES), API_CODES | GATEWAY_CODES)
        self.assertEqual({member.value for member in system_zero.ErrorCode}, API_CODES | GATEWAY_CODES)

    def test_namespace_equals_documented_api_and_helpers(self):
        self.assertEqual(set(system_zero.__all__), EXPECTED)
        self.assertEqual(len(system_zero.__all__), len(EXPECTED))
        actual = {name for name, value in vars(system_zero).items()
                  if (not name.startswith("_") or name == "__version__")
                  and not isinstance(value, types.ModuleType)}
        self.assertEqual(actual, EXPECTED)
        for name in EXPECTED:
            self.assertIsNotNone(getattr(system_zero, name))

    def test_client_resources_and_models_use_http(self):
        requests = []

        def respond(request):
            requests.append((request.method, request.url.path))
            return httpx.Response(200, json={"object": "list", "data": []},
                                  headers={"x-request-id": "surface-models"})

        with system_zero.SystemZero(api_key="surface-test", max_retries=0,
                                    transport=httpx.MockTransport(respond)) as client:
            self.assertEqual({name for name in vars(client) if not name.startswith("_")},
                             {"base_url", "timeout", "max_retries", "max_retry_wait", "retry_budget", "models"})
            self.assertEqual({name for name, value in vars(type(client)).items()
                              if not name.startswith("_") and callable(value)},
                             {"close", "decide", "decide_with_response"})
            self.assertEqual(client.models.list(), {"object": "list", "data": []})
        self.assertEqual(requests, [("GET", "/v1/models")])

if __name__ == "__main__":
    unittest.main()
