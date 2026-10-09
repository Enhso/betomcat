# betomcat: defence brief

What betomcat is, why it is built the way it is, and what the evidence says
about it so far. Updated at the end of every working session: sections 1 and 2
change only when the design changes; section 3 gets a new dated entry each
session, newest first, and old entries are left as written so the record shows
what was known when.

This repository is public. Nothing here may contain a forecast, rationale or
claim for a question that is still open.

Last updated: 2026-10-09.

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
   average of the two replaces it.
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

- Model weights are not computed yet: every draw is uniform. They matter once
  MiniBench round 2 resolves (15-19 Oct).
- Not built yet: the disagreement referee, Hatim's own forecasting history as
  context, the weekly digest, suggestions for merging families.
- The bot's own forecasts and their outcomes are never written back into the
  Workbench. They live only in the ledger, so the Workbench cannot yet show a
  model how the bot did on similar questions before.
- The research fallback covers the Workbench going down, not the news
  provider running out (see 2026-10-09 below).

## 3. What the evidence says

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

| Model | Kind | OK / attempts | Main failure |
|---|---|---|---|
| claude-fable-5.1 | paid | 22 / 25 | parser bug |
| claude-opus-5.5 | paid | 12 / 14 | parser bug |
| claude-sonnet-5 | paid | 16 / 20 | parser bug |
| gpt-6-astra | paid | 19 / 25 | parser bug, empty replies |
| gpt-6-sol | paid | 15 / 18 | parser bug |
| gpt-6-luna | paid | 21 / 24 | parser bug |
| gemini-3.8-flash | free (Google) | 1 / 38 | Google "high demand" errors |
| gemini-3.1-pro-preview | free (Google) | 0 / 6 | no free quota; disabled 30 Sep |
| nemotron-3-super | free | 16 / 17 | |
| nemotron-3-ultra | free | 15 / 25 | empty responses |
| dots-3-note | free | 7 / 27 | empty replies |
| qwen3.8-27b | free | 4 / 36 | no longer offered free |
| glm-5.2 | free | 0 / 3 | retired; disabled 30 Sep |

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
