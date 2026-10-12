# Cost (`CostPerTicketHigh`, `DailyBudgetNearlyExhausted`, `BudgetShedding`)

**What it means.** Either each ticket costs more than a cent (the measured cost is about a tenth of that), or today's spend is past 80 percent of the daily budget, or the budget is spent and the gateway is refusing every
caller except the exempt ones (the monitor).

**Who is affected.** At 100 percent, tickets are held with a `budget_exhausted` reason; nothing is lost and nothing wrong is sent.

## The first five minutes

```bash
curl -s -H "X-Service: monitor-agent" -H "X-Service-Key: $MONITOR_KEY" $GATEWAY/v1/status      # spend.usd against spend.budget
curl -s -H "X-Admin-Key: $ADMIN_KEY" $TICKETS/admin/releases                                  # what is serving
# Prometheus: sum(rate(ai_gateway_cost_usd_total[5m])) / sum(rate(ai_ticket_outcomes_total[5m]))        # dollars per ticket
# Prometheus: sum by (direction) (rate(ai_gateway_tokens_total[5m]))                                    # input vs output tokens
```

## Likely causes and what to do

- **Input tokens per ticket jumped.** A release made a prompt longer. This is the common one: roll the release back, `POST $TICKETS/admin/releases/rollback`. The gate warns when tokens rise more than 25 percent
  against the baseline; a warning is not a block, so read the warnings.
- **More model calls per ticket.** A tool loop that does not end, or retries. Look at `ai_gateway_requests_total` per ticket and the `requisite_agent_tool_calls_total` rate.
- **Traffic is simply higher.** Then the budget is doing its job; raising it is the owner's decision.
- **Heavy model used for everything.** A classifier or pin change sent traffic to the expensive tier (about 58 times the light call in the lab's measurement); check `ai_gateway_requests_total` by tier.

If spend is running away and you cannot find the cause in five minutes, freeze intake: `POST $TICKETS/admin/freeze`. The cheapest incident is the one that stops spending first.

## Escalate when

spend continues after a rollback and a freeze, which would mean something other than the ticket path is calling the gateway.

## Afterwards

Add the release's token count per ticket to the review of any prompt change.
