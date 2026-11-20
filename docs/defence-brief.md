# betomcat: defence brief

What betomcat is, why it is built the way it is, and what the evidence says
about it so far. Updated at the end of every working session: sections 1 and 2
change only when the design changes; section 3 gets a new dated entry each
session, newest first, and old entries are left as written so the record shows
what was known when.

This repository is public. Nothing here may contain a forecast, rationale or
claim for a question that is still open.

Last updated: 2026-11-20.

## 1. What betomcat is

betomcat is an autonomous forecasting bot. It forecasts under the Metaculus
bot account `vezo3` in the Fall 2026 FutureEval bot tournament (questions from
28 Sep 2026 to 6 Jan 2027) and in MiniBench, the fortnightly warm-up rounds.
The rules: no human touches a live forecast, only the last forecast before a
question closes is scored, and every forecast carries a private comment.

It is built together with a second system, the Intelligence Workbench (IW, a
separate private repository). The Workbench does the research: it fetches
news and reference articles, stores each document with the time it was
fetched and a fingerprint of its content, breaks documents into individual
claims that keep their source, scores how well each claim is supported, and
sorts questions into topic "families". It never produces a probability. The
bot reads the Workbench's research and does the forecasting.

A second bot, `vezocontrol`, runs the Metaculus starter template on the same
questions. It is the control: the season's main test is whether betomcat
beats it.

What happens when a question opens:

1. A shift (a 5.5-hour job on GitHub Actions) sees the question and claims it.
2. The Workbench researches it: two AskNews news searches and one AskNews
   wiki search, claim extraction and support scoring by a cheap model, and a
   family tag.
3. Two models are drawn at random from a pool of about a dozen (Anthropic,
   OpenAI and Google models, plus free models at equal odds). Once enough
   questions have resolved, better-scoring models will be drawn more often.
4. Both models forecast from the same research. The first forecast in is
   submitted at once as a provisional; when the second arrives, the weighted
   average of the two replaces it. If the two disagree widely and a
   classifier finds a fixable cause (stale research or a misread resolution
   rule), the bot researches the disagreement and both models forecast once
   more; the weighted average of the second round is the final.
5. One comment is posted with the forecast that stands: a rationale of at
   most 300 words, written by a cheap model from the two models' reasoning.
   The full record (both rationales, claims used, costs, errors) goes into an
   encrypted ledger, never into the public logs.

## 2. Why it is built this way

Each choice, the reason for it, and the price paid.

**The research layer never forecasts.** Research cannot drift toward an
answer, and the same research is reusable outside the tournament. Price: two
systems to run instead of one.

**Evidence is kept as it stood at each moment, never overwritten.** Every
document carries its fetch time and a content hash, and the database keeps
every version. It can answer "what did the evidence look like at time T",
which is what makes a later backtest honest: nothing learned after a question
closed can leak into a replay of it. Price: storage only grows.

**Two randomly drawn models, not an ensemble of personas.** An earlier
persona ensemble cost a lot and showed no gain. Two models keep the cost per
question low, and drawing from a mixed pool makes the season an experiment on
which models forecast well. Free models are drawn at the same odds as paid
ones. Price: quality varies from one question to the next.

**The final number is mechanical.** It is the average of the two forecasts,
weighted by each model's pool weight as frozen at the moment of the draw.
Nothing overrides that arithmetic. A planned "referee" may classify why two
models disagree and order more research, but it can never change the number.
Every forecast can be audited, and no faulty component can bend a submission.

**Expensive models only for the two forecasts.** Claim extraction, scoring,
family tags and the comment all run on cheap or free models. The whole budget
is $100 of OpenRouter credit from Metaculus.

**Never miss a question, never invent a number.** Submit as soon as one
forecast exists; 30 minutes before close, retry any model that has not
answered; 5 minutes before close, submit whatever exists. If nothing exists,
the question is missed: no default 50%, no copy of the crowd. An invented
number would corrupt the performance data that the model weights and the
comparison with the control depend on.

**Hosted on GitHub Actions in self-chaining shifts.** The plan called for an
always-on server; the free one required a credit card, which was not
available. Each shift runs about 5.5 hours, saves its state (encrypted) every
15 minutes and at the end, and starts its successor. A scheduled check every
30 minutes restarts the chain if it breaks. Price: GitHub's own failures
become the bot's, and because the repository is public the logs are too, so
they are redacted to question ids and statuses.

**One short comment per question.** Metaculus penalizes long comments, so
instead of the full pipeline dump first planned, the comment is a single
rationale of at most 300 words that opens with the submitted number. The full
audit record stays in the ledger.

**A separate budget for each MiniBench round.** About half the credit was set
aside for MiniBench round 2 ($50 over its busy days), the rest for the main
tournament.

### Where it departs from the plan today

- Model weights score each model against the other forecasters on the
  question, not against the community prediction as planned: Metaculus hides
  the community prediction from bot accounts. The field's average is backed
  out of the bot's own peer score, which is also what the tournament ranks
  on. Weights went live on 23 Oct.
- Not built yet: Hatim's own forecasting history as context, the weekly
  summary email (built 23 Oct, not yet deployed), suggestions for merging
  families. The disagreement referee went live on 23 Oct.
- The bot's own forecasts and their outcomes are never written back into the
  Workbench. They live only in the ledger, so the Workbench cannot yet show a
  model how the bot did on similar questions before.
- The research fallback covers the Workbench going down, not the news
  provider running out (see 2026-10-09 below).

## 3. What the evidence says

### 2026-11-20 (tail backtest; binary calibration; shifts on the merged code)

**Scope.** Two measurements from the ledger snapshot of 20 Nov and one from
the shift logs. No new Metaculus standing was read. Sections 1-2 stand: the
prompt and the reconciliation are unchanged. The backtest is a new offline
module (`betomcat.backtest`). It makes no model calls and changes nothing
live.

**The backtest reproduces what was scored.** It re-reads each model's saved
response and rebuilds that model's distribution with the bot's own parser.
It then combines the two models with their frozen weights. On all 79
resolved numeric and discrete questions, the rebuilt distribution matches the
submitted one exactly. The four worst FE questions each score -106.5 against
the baseline, as the mechanism predicts. The fourth of them had an open lower
bound at exactly $110M and resolved at $109.9M, so it too resolved out of
range.

**Wider tails would have scored better, but the sample cannot settle it.** 6
of the 79 questions resolved outside the range (7.6%; exact 95% interval
2.8-15.8%). Peer points against today's prompt, over all 79:

| Variant | Total change | 90% interval | Out-of-range gain (6 q) | In-range cost (73 q) | Break-even out-of-range rate |
|---|---|---|---|---|---|
| 10th-90th percentiles | +176 | -192 to +587 | +463 | -286 | 4.8% |
| 5th-95th percentiles | +79 | -198 to +375 | +292 | -213 | 5.7% |
| 5% mixed with a flat distribution | +28 | -32 to +93 | +63 | -35 | 4.4% |
| 10% mixed | +31 | -75 to +147 | +111 | -80 | 5.6% |
| 20% mixed | -9 | -191 to +190 | +180 | -190 | 8.0% |

Intervals are a bootstrap over research families (65 clusters). Both sides
of the trade are concentrated. About 427 of the 10th-90th variant's +463
out-of-range gain comes from the 4 questions that had 0.594% beyond the
bound. The in-range cost has a median of 0, and 94% of it comes from 10
questions. In 9 of those 10, the outcome fell outside at least one model's
10th-90th range. A narrower variant pays when more than about 5% of outcomes
land out of range. The observed rate is above that, but its interval
includes it. The 10th-90th variant is also the best of five chosen on the
same data. The narrower variants are mechanical counterfactuals. They drop
the outer percentile lines from the saved responses. Models asked for fewer
percentiles might state the remaining ones differently.

**Binary forecasts lean underconfident, short of significance.** The full
ledger does not show the narrow range seen on the 13 FE questions:
forecasts span 0.015-0.900. On all 79 resolved binaries, the 24 forecasts
between 0.1 and 0.3 averaged 0.17 and 1 resolved yes (4%). The 20 forecasts
between 0.5 and 0.7 averaged 0.59 and 16 resolved yes (80%). Pushing each
forecast away from 50% on the log-odds scale (factor a) would have gained
peer points: a = 1.25 +103 (90% interval -14 to +207), a = 1.5 +141 (-92
to +348). The best-fitting factor is 1.56, but its 95% interval
(0.92-2.40) includes 1, no change, and it was fitted on the same 79
questions it is scored on. Averaging two models' probabilities pulls the
result toward 50%, which would produce this lean (inferred, not tested).
Extremizing would change section 2's rule that the final number is the
plain weighted average. That is a design decision, and the evidence for it
is not yet conclusive.

**Shifts on the merged code.** All 13 host shifts since the 17 Nov merge
passed their test gate (shift logs). The other 10 runs in that window were
watchdog checks that found the chain alive. In the ledger, 21 runs since
then are all submitted,
none degraded, and each has two successful models. One was a referee re-run
with four. 18 of 62 model calls failed, all on free models. One newly added
free model rejects every call (OpenRouter serves it only to agent apps). A
change that disables it is built but not live.

**What it changes for the argument.** The tail effect is real per question
and visible in the scored submissions. Its net size over a season depends on
how often outcomes leave the range, and 79 questions cannot pin that rate
down. On binaries, the full ledger shows a lean toward underconfidence that
is consistent with averaging two models but is not significant at 95%; it
needs rechecking as more binaries resolve. Both point at how betomcat turns
model answers into a submission rather than at research gaps.

### 2026-11-17 (FE Fall standing; the 23 Oct deployments in live shifts)

**Scope.** Two new measurements. The ledger snapshot of 17 Nov covers every
shift since 23 Oct. Metaculus's FE Fall leaderboard (project 33121) was
read on 17 Nov with the `metaculus-bot-review` tool. Sections 1-2 stand: no
design changed. A forecasting-tools bump and a startup key check are built
on a branch and are not yet live.

**The 23 Oct deployments work.** Research is back. In the week of 19 Oct,
71 of 83 runs were degraded; since 24 Oct, 2 of 140 are. Every run since
24 Oct ended with two successful models. The referee examined 22 runs and
re-researched 2. Model weights update daily. Of 353 model calls since
24 Oct, 49 failed, nearly all on free models that were unavailable or
returned empty replies; one failed to parse.

**FE Fall standing.** betomcat ranks 360 of 490 with a spot peer score of
-811.9, on 32 scored questions out of 122 forecast. Among the 214 bots with
coverage of 28 or more it ranks 197th. Metaculus's reference bots run the
same models betomcat draws, each alone with AskNews research, and score far
higher: Claude Sonnet 5.5 +475.6, Claude Opus 5.5 +445.4, Claude Fable 5.1
+586.5. The field median is +47.6.

**Where the points went.** By question type (questions, summed spot peer
score): numeric 5, -336.6; discrete 10, -192.0; binary 13, -136.1; multiple
choice 4, -147.3. The four worst questions are numeric or discrete and score
-167, -143, -117 and -117. Each has a baseline score of exactly -106.5.
Metaculus marks three as resolved outside the question's range. The fourth
resolved at $109.9M, just under the roughly $110M lower bound the models
cite. So it is likely out of range as well, though its bounds were not
fetched.

**Mechanism (verified by arithmetic, not yet by backtest).** The numeric
prompt asks for the 1st to 99th percentiles. Models keep the 99th inside an
open bound. forecasting-tools then places the bound at the 99.5th percentile
and, after its minimum-mass adjustment, leaves 0.594% of probability beyond
each open bound. The submitted distributions show exactly that. 50 x
ln(0.00594 / 0.05) = -106.5. Asking for the 10th to 90th percentiles, as the
template does, leaves about 5% beyond the bound. There an out-of-range
outcome scores about 0.

**What it changes for the argument.** The gap to the reference bots is too
large to blame on missing Workbench research. Part of it is a mechanical
property of how betomcat states numeric distributions. A backtest on all
resolved numeric questions in the ledger comes next, comparing tail
variants without new model calls. Until it runs, the size of the effect is
an estimate. A second pattern is noted, not tested. All 13 scored binary
forecasts lie between 0.095 and 0.594, and the losses fall where the field
was confident and right.

### 2026-11-10 (upstream template sync; no new live measurements)

**Scope.** The forked template (`Metaculus/metac-bot-template`) published 10
commits after our fork point. They are merged on a local branch. betomcat's
own code, prompts and design are unchanged, so sections 1-2 stand. No ledger
or Metaculus results were fetched.

**Recorded evidence.** The control's Actions run list on 10 Nov shows its
shifts still running. Of its last four finished shifts, three succeeded and
one was cancelled (10 Nov, 03:39 UTC start). The control holds its own
OpenRouter key, so upstream's removal of the Metaculus LLM proxy does not
touch the baseline.

**What it changes for the argument.** One upstream fix names a failure mode
betomcat shares. The SDK's numeric parser collects every percentile line in a
response. If a model writes a draft distribution before its final answer, the
parser reads the draft and the final answer together. We have not measured
whether this caused parse retries or wrong distributions on live questions.
The next step is a ledger count of numeric parse failures and a regression
test (HANDOFF s5.9).

### 2026-11-08 (evaluation proposal; experiments not run)

**Scope and existing evidence.** This is an experimental-design review, not
an accuracy result or a production change. No new live data were fetched.
The 23 Oct ledger review counted 183 submissions, 137 without research;
shift logs for 19-23 Oct showed successor starts within a minute. The
recorded finalized MiniBench round-2 ranks were 49/306 for betomcat and
186/306 for the control, which covered 32/57 scored questions. These are
historical figures, not current measurements or matched experimental arms.

**Workbench test.** Generate paired, prospective forecasts with the same
models, frozen weights, forecasting instructions and deadlines: direct
research versus IW-structured evidence. Use the same fetched documents to
isolate structuring; add a question-only sanity baseline and an IW-without-
family-history ablation when budget permits. If retrieval differs too,
label the comparison end-to-end rather than attributing it to structuring.

**Loop test.** Freeze the first-round two-model ensemble and determine
eligibility before branching. Compare keeping that forecast with the
current targeted-research policy. Controls that reforecast on unchanged
evidence, and on a generic refresh with a matched research budget, separate
the value of research from another model call and targeted search from
ordinary fresh information. Keep the diagnosis shared across reforecast
controls. Count failed or late reruns with the policy's actual fallback;
do not analyze only successful reruns or compare triggered questions with
untriggered ones.

**Leakage controls.** Prefer automated forecasts saved before outcomes are
known. Archive the question state, rendered prompts, full evidence snapshots,
model/configuration versions and stage cutoffs. Freeze all retrieval and
history as of the relevant stage; later stages may use newly fetched
information only before their deadline. Isolate experimental writes from
other arms and production. Historical document timestamps do not remove
outcome knowledge from a newer model's training. Tune on earlier periods,
then freeze the protocol for a later holdout; only resolutions already
available may inform weights or retrieved outcome history.

**Evaluation.** Predeclare paired improvement under the exact tournament
scoring rule as the primary competitive comparison. Use log loss, binary/
categorical Brier score and numeric/date CRPS as appropriate diagnostics;
report calibration, coverage, deadline success and full incremental cost.
Estimate uncertainty over independent questions/events, clustering related
families rather than treating model calls as independent samples. Keep
pending and annulled outcomes accounted for. Do not use leaderboard rank,
reduced disagreement or the weight learner's clipped scores as the primary
accuracy test. Set the minimum worthwhile gain and stopping rule before the
holdout, and distinguish an inconclusive interval from evidence of no gain.

**Instrumentation needed.** The ledger retains both rounds, but its
last-successful-model scoring view loses stage distinctions. Evaluation
needs explicit arm/stage identities, forecast lineage, immutable question
and prompt inputs, and both initial and refreshed research snapshots;
`runs.dossier_id` currently retains the initial dossier after a rerun.
These changes and the experiments above are proposed, not implemented.

### 2026-11-08 (failure-hypothesis review; no new live measurements)

**Scope.** Source and documentation review only. No code or design changed;
no live ledger, shift logs or Metaculus results were fetched. The numbers
below come from the earlier dated entries, not a check of today's system.

**Recorded evidence.** The 23 Oct ledger review reported 183 submissions,
137 without research. Shift logs for 19-23 Oct showed each successor
starting within a minute of the previous shift ending. Metaculus's finalized
MiniBench round-2 result was rank 49 of 306, peer score 272, with a rank
interval of 14-109; the control ranked 186 and covered 32 of 57 scored
questions. The 9 Oct family count was 68 families for 75 questions, with
only 7 families containing more than one question.

**Candidates to test, not findings of forecast error.**

1. Claim support measures what excerpts say, not whether a claim is true or
   useful for predicting the resolution. Extraction may remove qualifications,
   and repeated reporting may look like independent corroboration. Compare
   structured claims with their underlying documents using the same models
   and archived evidence cutoff.
2. Both forecasters read the same prompt and research. Shared mistakes can
   produce agreement and bypass the disagreement-triggered referee. Audit
   agreed-but-wrong resolved forecasts as well as disagreements, and measure
   whether the ensemble improves on its individual members.
3. Weights become performance-based after 10 scored questions per model.
   A small or topic-concentrated sample may reward luck rather than durable
   skill. Compare adaptive and uniform policies on subsequent questions,
   respecting when resolutions became available and grouping related
   questions when estimating uncertainty.
4. Beating the control tests the bundled system, not the Workbench alone:
   models and coverage differ too. A matched research ablation is needed to
   attribute any gain to the Workbench; report coverage separately.
5. The archive may not compound if families fragment or questions rarely
   recur. Check family assignments on resolved questions and measure useful
   retrieval and forecast improvement, not merely stored-document counts.

These tests were proposed, not run. The review does not establish whether
research recovery or the 23 Oct deployments worked in later live shifts.

### 2026-10-23 (covers 10 to 23 Oct)

**In short.** The first scores are in. betomcat ranked 49th of 306 bots in
MiniBench round 2, and the control ranked 186th. The result is weaker than
it looks: 23 of betomcat's 59 round-2 forecasts had no research, the control
covered only 32 of the 57 scored questions, and the two bots run different
models. From 10 to 22 Oct betomcat forecast without research,
because the Workbench update that restores it was merged but never deployed.

**MiniBench round 2 (opened 5-7 Oct, finalized).** 57 questions were scored.
betomcat: rank 49 of 306, peer score 272 (Metaculus's interval 60 to 483,
rank interval 14 to 109), no prize. The control: rank 186, score -403, on 32
of the 57. The top seven bots scored 556 to 699.

**FE Fall.** Two questions have resolved, too few to read anything into.

**Research.** All 107 of betomcat's forecasts from 10 to 22 Oct used only the
question text, including all 58 of MiniBench round 3 (opened 19 Oct). Over
the season, 137 of its 183 submissions had no research. The Workbench's
free-source update (Google News, Bing News, Wikipedia, 1-call AskNews) was
merged on 11 Oct but not pushed, so every shift kept running the old
AskNews-only code. The AskNews wallet refilled on 22 Oct, and the first
forecasts with research since 5 Oct came on 23 Oct, on the old code that
spends about 11 AskNews calls a question.

**The control in round 3.** It has forecast 21 of the 58 questions so far,
against betomcat's 58, although it has had the free-source blend since 9 Oct.

**Models.** Seven pool models now have at least 10 resolved questions, the
point at which weights leave uniform: gpt-6-astra and claude-fable-5.1 (18
each), gpt-6-luna (17), claude-sonnet-5 (14), nemotron-3-ultra (13), gpt-6-sol
and nemotron-3-super (12). The weight update is not built, so every draw is
still uniform.

**Cost.** $69.64 of credit is left ($81 on 9 Oct). Forecast calls in the
ledger cost $9.81 over the 107 questions to 22 Oct, about $0.09 each without
research; the two forecasts with research on 23 Oct cost about $0.37 each
(two runs only; the Workbench's own calls on the same key come on top). The budget
guard's pacing horizon ended on 19 Oct, so it no longer limits daily spend,
and the $50 round allowance is now most of what is left.

**Hosting.** From 19 to 23 Oct every shift started within a minute of the
previous one ending.

**What a sceptic should press on.**

1. One round, a wide interval (rank 14 to 109), and the confounds above.
2. Round 3 reverses the confound: betomcat without research, the control
   with it. Its score says little about the Workbench either way.
3. Three quarters of betomcat's record so far was made without the research
   layer the design rests on.

**Watch next.** Deploying the Workbench update before the AskNews wallet runs
dry again; MiniBench round 3 scores (round closes 6 Nov).

### 2026-10-09 (covers 24 Sep to 9 Oct)

**In short.** The machinery works: betomcat reached every question that
opened and submitted on 74 of 75. Whether its forecasts are any good is
unknown, because none of them has resolved. Three things weaken the test it
is running: since 5 Oct, 30 of its forecasts were made with no research at
all and the control bot stopped forecasting entirely; the control runs a much
weaker model, so even a clear win would not show that the Workbench is what
helped; and a parser bug cost it one question (fixed the same day).

**Coverage.** 75 questions opened: 16 in FE Fall (28 Sep to 9 Oct) and 59 in
MiniBench round 2 (5 to 7 Oct). betomcat submitted on 74. Median time from
claiming a question to its final forecast: 1.9 minutes (90% within 5.2).

**The one miss (FE Fall, multiple choice, closed 9 Oct).** Every model it
tried answered. The forecasting-tools parser rejected every answer: two of the
options were named "4" and ">4", and when the parser looks for the line giving
option "4" it finds two. All spare models were used up in six minutes, and
the 30-minute retry failed the same way. The shift ended a few minutes later;
the next shift claimed the question again, its two models failed the same
way within a minute, and the 5-minute cutoff passed with nothing to submit.
This is a systematic bug, not bad luck: it
fails any multiple-choice question where one option's name appears inside
another's. It accounts for 21 of the 41 failed attempts on multiple-choice
questions.

**Research outage.** The AskNews wallet ran dry on 5 Oct around 18:00 UTC
(`402: Your wallet balance is depleted`), in the middle of MiniBench round 2.
Both bots use the same AskNews key, and the control logged the same error
from 17:54. Since then 30 of betomcat's 74 submissions (23 MiniBench, 7 FE
Fall) were forecast from the question text alone. The fallback was designed
for the Workbench going down: it then calls AskNews directly. With AskNews
itself gone, both paths come back empty. Research is the one stage with no
working backup.

**The control bot.** It forecast 37 of the 44 questions that opened before
the wallet ran dry, and none of the 31 since: without research, the template
fails outright instead of forecasting. Until research is restored, the
comparison is limited to those 37 questions, all with news on both sides.

**Models.** Successful attempts out of all attempts:

| Model                  | Kind          | OK / attempts | Main failure                   |
| ---------------------- | ------------- | ------------- | ------------------------------ |
| claude-fable-5.1       | paid          | 22 / 25       | parser bug                     |
| claude-opus-5.5        | paid          | 12 / 14       | parser bug                     |
| claude-sonnet-5        | paid          | 16 / 20       | parser bug                     |
| gpt-6-astra            | paid          | 19 / 25       | parser bug, empty replies      |
| gpt-6-sol              | paid          | 15 / 18       | parser bug                     |
| gpt-6-luna             | paid          | 21 / 24       | parser bug                     |
| gemini-3.8-flash       | free (Google) | 1 / 38        | Google "high demand" errors    |
| gemini-3.1-pro-preview | free (Google) | 0 / 6         | no free quota; disabled 30 Sep |
| nemotron-3-super       | free          | 16 / 17       |                                |
| nemotron-3-ultra       | free          | 15 / 25       | empty responses                |
| dots-3-note            | free          | 7 / 27        | empty replies                  |
| qwen3.8-27b            | free          | 4 / 36        | no longer offered free         |
| glm-5.2                | free          | 0 / 3         | retired; disabled 30 Sep       |

Paid models fail rarely, and mostly because of the parser bug rather than the
model. Free models other than nemotron-3-super fail often, and two that are
still enabled (gemini-3.8-flash and qwen3.8-27b) almost always fail. A
replacement draw stepped in on 27 of 75 questions. It is what makes free
models at equal odds survivable, and it means paid models carry more of the
load than the uniform draw suggests.

**Cost.** Forecast calls cost $15.06 over 74 questions, about $0.20 each.
MiniBench round 2 used about $12 of its $50 allowance. $81 of credit remains
(from $99.33 on 22 Sep); the ledger's total leaves out the comment and
Workbench calls. Money is not the binding constraint; AskNews is.

**Hosting.** From 3 to 9 Oct every shift started within a minute of the
previous one ending. Two shifts (3 and 7 Oct) crashed when GitHub failed to
accept a state upload. The successor started anyway, and the ledger shows no
question lost.

**Families.** 75 questions produced 68 families; only 7 families hold more
than one question. The plan assumed the tournament keeps asking about the
same topics, so stored research would be reused. So far it rarely is.

**Accuracy.** Nothing has resolved, so there is no evidence on forecast
quality, on which models are better, or on betomcat against the control.
MiniBench round 2 resolves around 15-19 Oct.

**What a sceptic should press on.**

1. No scored forecasts yet: everything above is about the machinery.
2. The comparison is confounded. The control runs Gemma 4 31B (free) for
   every step; betomcat draws frontier models. If betomcat wins, better models
   and the Workbench are mixed together. The outage gives a rough comparison
   within betomcat (44 forecasts with research, 30 without), but not a clean
   one: the two groups are different questions asked at different times.
3. Since 5 Oct, 30 of betomcat's forecasts had no research, and the control
   made no forecasts at all.
4. Family reuse is low, so the Workbench's compounding value has not started.
5. Known holes: research with a single provider (shared by both bots), and
   the bot's record not flowing back into the Workbench.

**Fixed the same day.** Multiple-choice answers now go to a backup parser
when the library's parser rejects them. It matches option names exactly, and
it recovers 19 of the 21 answers rejected on the missed question; the other
two were unusable. gemini-3.8-flash and qwen3.8-27b were taken out of the
pool, and Anthropic's two new models (claude-sonnet-5.5, claude-haiku-5.5)
were added after a test call each: 8 paid and 3 free models. A weekly check
against OpenRouter's catalog will propose further additions and removals as a
pull request, so the pool no longer depends on noticing releases by hand.

**Research backup, half shipped.** AskNews's quota turned out to be 1,000
calls a month shared by both bots, and the old searches cost about 11 calls
a question (v1) and 6 (v2), so the cap, not a leak, emptied it. The fix
blends free sources (Google News headlines, Bing News, Wikipedia) into every
research and keeps AskNews only for 1-call latest-news searches, split
750/250 a month between betomcat and the control. The control's version is
deployed. betomcat's is built and tested on a live question (16 sourced
claims with AskNews down) but not yet deployed, so its forecasts still run
without research until the next session ships it.

**Watch next.** First scores when MiniBench round 2 resolves (15-19 Oct); the
model weights must exist by then. The AskNews wallet refills on 22 Oct.
