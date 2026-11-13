import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { decisionFor, decisionsFor } from "../src/decisions.js";
import { policyDecisionFor, validatePolicy } from "../src/policy.js";
import { scoreReply } from "../src/evaluate.js";
import { SystemZeroClient } from "../src/client.js";
import { runFlow } from "../src/flows.js";

const fixtures = JSON.parse(readFileSync(new URL("../../fixtures/policy/answers.json", import.meta.url), "utf8"));
describe("released policy scope and shared readers", () => {
  it.each([
    [{ on_low_confidence: "error" }, "policy.on_low_confidence"],
    [{ require_calibrated: true }, "policy.require_calibrated"],
    [{ require_calibrated: false }, "policy.require_calibrated"],
  ])("refuses held request members %j", (policy, param) => {
    expect(() => validatePolicy(policy)).toThrow(expect.objectContaining({ code: "invalid_request", param }));
  });
  it.each(fixtures.abstained)("reads $name through the ordinary reader without recovering a decision", ({ answer }: { answer: never }) => {
    expect(decisionFor(answer)).toEqual(policyDecisionFor(answer));
    expect(decisionFor(answer)).not.toHaveProperty("decision");
    expect(decisionsFor({ answers: { q: answer } } as never).q).toMatchObject({ kind: "abstained" });
  });
  it("rejects mixed legacy and abstained successes", () => {
    expect(() => decisionsFor({ answers: { a: fixtures.abstained[0].answer, b: fixtures.legacy } } as never)).toThrow(/mix/u);
  });
  it("scores abstentions separately without claiming correctness or calibration", () => {
    const request = { questions: { q: { type: "boolean" as const, instructions: "SYNTHETIC" } } };
    const scores = scoreReply(request, { q: true }, { answers: { q: fixtures.abstained[0].answer } } as never);
    expect(scores[0]).toMatchObject({ outcome: "abstained", correct: false, confidence: null, code: null });
  });
  it("lets an abstained flow use its default branch without inventing a decision", async () => {
    const reply = { id: "szd_synthetic", model: "rightnow-decision", served_by: "synthetic", created: 1,
      context: { route: "direct", tokens: 1 }, answers: { q: fixtures.abstained[0].answer },
      usage: { input_tokens: 1, cached_input_tokens: 0, media_tokens: 0, long_input_tokens: 0, media_items: 0, thought_tokens: 1 } };
    const client = new SystemZeroClient({ apiKey: "SYNTHETIC", maxRetries: 0, timeout: 1000, fetch: async () => Response.json(reply) });
    const trace = await runFlow(client, { version: 1, request: { model: "rightnow-decision", state: "SYNTHETIC" },
      steps: [{ id: "first", questions: { q: { type: "boolean", instructions: "SYNTHETIC" } }, next: { rules: [], default: null } }] },
    { run_id: "policy-on-flow", deadline_ms: 10000, include_content: true, now: () => 0 });
    expect(trace.status).toBe("completed");
    expect(trace.steps[0]!.answers[0]).toMatchObject({ kind: "abstained" });
    expect(trace.steps[0]!.answers[0]).not.toHaveProperty("decision");
  });
});
