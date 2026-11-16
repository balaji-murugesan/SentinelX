"""The two ceilings the order-rate caps may never exceed (order-rate-limiter spec "A hard ceiling
that configuration cannot raise").

They are constants in code, not configuration, so no environment variable can raise them:
``EngineSettings`` refuses to start with a rate above its ceiling, and ``RateLimitedAdapter``
refuses to be built with one. This is a leaf module (it imports nothing) so that ``engine.config``
can read the ceilings without importing the limiter, which imports the broker and orders packages.

Trading terms:
  * SEBI threshold - since the retail-algo framework, a self-built algo needs exchange
                     registration above 10 orders per second per exchange. The engine stays
                     well below it (PRD §10).
  * Dhan's limits  - the order API allows 10/second, 250/minute, 1,000/hour and 7,000/day
                     (PRD §10, to be re-verified before live trading).
"""

# The values are proposals from add-risk-engine's design (Open Questions) for the owner to
# confirm. Change them here only with the owner's agreement, and never to 10 or more per second.
ORDERS_PER_S_CEILING = 5  # well below SEBI's 10 per second
ORDERS_PER_MIN_CEILING = 200  # below Dhan's published 250 per minute
