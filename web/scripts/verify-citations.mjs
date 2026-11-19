/**
 * Unit checks for the citation helpers behind the answer badges.
 *
 * These are pure functions, so this runs without a DOM: the rendering path is
 * covered by the mock server's `MOCK_CITATIONS=1` fixture (see mock-server.mjs).
 *
 *   npm run verify:citations
 */

import {
  collectQueries,
  collectSources,
  hostOf,
  injectCitationMarkers,
  sliceCitations,
  toCitationRanges,
} from "../src/lib/citations.ts";

const failures = [];
let assertions = 0;
const check = (name, actual, expected) => {
  assertions += 1;
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) failures.push(`${name}\n  actual:   ${a}\n  expected: ${e}`);
};

/* -------------------------------------------------------------------------- */
/* Ranges                                                                      */
/* -------------------------------------------------------------------------- */

const annotations = [
  { type: "url_citation", url_citation: { url: "https://a.test", title: "A", start_index: 0, end_index: 4 } },
  // Same URL again: it must share ordinal 1 rather than reserving a new number.
  { type: "url_citation", url_citation: { url: "https://a.test", title: "A2", start_index: 4, end_index: 9 } },
  { type: "url_citation", url_citation: { url: "https://b.test", title: null, start_index: 9, end_index: 12 } },
  // Malformed entries must be ignored, not throw.
  { type: "url_citation", url_citation: { start_index: 0, end_index: 1 } },
  { type: "url_citation", url_citation: { url: "https://c.test", start_index: 5, end_index: 5 } },
  {},
];

const ranges = toCitationRanges(annotations);
check(
  "ranges keep ordinals per url and drop malformed spans",
  ranges.map((range) => `${range.ordinal}@${range.start}-${range.end}`),
  ["1@0-4", "1@4-9", "2@9-12"],
);

/* -------------------------------------------------------------------------- */
/* Marker injection                                                            */
/* -------------------------------------------------------------------------- */

const text = "ABCDEFGHIJKL";
const injected = injectCitationMarkers(text, ranges);
check("only the first span of a url gets a badge", injected.badges.length, 2);
// A badge goes *after* the span it cites; the second URL's span runs to the end.
check(
  "badges land at the end of their span",
  injected.html,
  'ABCD<sup class="wb-cite" data-cite-idx="0">1</sup>EFGHIJKL<sup class="wb-cite" data-cite-idx="1">2</sup>',
);

// A span ending inside an inline code span is skipped: markup there would
// render as literal text.
const codeText = "AB`CD`EF";
const codeRanges = toCitationRanges([
  { url_citation: { url: "https://d.test", start_index: 0, end_index: 5 } },
]);
check("spans inside code are skipped", injectCitationMarkers(codeText, codeRanges).badges.length, 0);

// A span ending past the text is clamped away rather than slicing out of range.
const longRanges = toCitationRanges([
  { url_citation: { url: "https://e.test", start_index: 0, end_index: 999 } },
]);
check("spans past the end are dropped", injectCitationMarkers(text, longRanges).badges.length, 0);

check("empty input is a no-op", injectCitationMarkers("", ranges), { html: "", badges: [] });

/* -------------------------------------------------------------------------- */
/* Slicing per text block                                                      */
/* -------------------------------------------------------------------------- */

check(
  "a later block receives re-based offsets",
  sliceCitations(annotations, 4, 6).map((item) => {
    const citation = item.url_citation;
    return `${citation.start_index}-${citation.end_index}`;
  }),
  ["0-5", "5-6"],
);
check("blocks that no span touches get nothing", sliceCitations(annotations, 100, 10), []);

/* -------------------------------------------------------------------------- */
/* Sources                                                                     */
/* -------------------------------------------------------------------------- */

const rounds = [
  {
    calls: [
      {
        action: { queries: ["q1", "q1", "q2"] },
        sources: [{ url: "https://a.test" }, { url: "https://a.test" }, { url: "https://b.test" }],
      },
    ],
  },
];
check("sources are deduplicated by url", collectSources(rounds).length, 2);
check("queries are deduplicated", collectQueries(rounds), ["q1", "q2"]);
check("missing rounds yield nothing", collectSources(undefined), []);
check("hosts drop the www prefix", hostOf("https://www.example.com/x"), "example.com");
check("a non-url falls back to itself", hostOf("not a url"), "not a url");

/* -------------------------------------------------------------------------- */

if (failures.length > 0) {
  console.error(`Citation helper checks FAILED (${failures.length} of ${assertions})`);
  for (const failure of failures) console.error(" -", failure);
  process.exit(1);
}
console.log(`Citation helper checks PASS (${assertions} assertions)`);
