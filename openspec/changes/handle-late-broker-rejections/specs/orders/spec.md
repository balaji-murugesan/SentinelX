# Spec Delta

## MODIFIED Requirements

### Requirement: Order state machine
Orders SHALL move only along the transitions of PRD §5. These are Requested → Submitted or Rejected; Submitted → Open, Rejected or Unknown; Unknown → Open or Rejected; Open → PartiallyFilled, Filled, Cancelled or Rejected; PartiallyFilled → Filled or Cancelled. Filled, Rejected and Cancelled are final. Any other transition SHALL be refused, logged with the order id, and leave the order unchanged.

#### Scenario: Update after a final state
- **WHEN** a fill arrives for an order that is already `Cancelled`
- **THEN** the order stays `Cancelled`, the event is logged as an invalid transition, and the fill is recorded for reconciliation but changes no position

#### Scenario: Rejection after a partial fill is refused
- **WHEN** a broker rejection arrives for an order that is `PartiallyFilled`
- **THEN** the order stays `PartiallyFilled` with its filled quantity, and the event is logged as an invalid transition

## ADDED Requirements

### Requirement: A broker rejection after the acknowledgement is applied
When the broker rejects an order it had already acknowledged, the engine SHALL move the order from `Open` to `Rejected`, record the broker's reason, store the transition as an audit record, and report it as a broker rejection. From then on the order SHALL NOT count as resting anywhere the engine reads resting orders, including the lot-cap and exposure reservations of pending entries.

#### Scenario: Acknowledged entry rejected by the broker
- **WHEN** an entry has been acknowledged by the broker (`Open`) and the broker then sends a rejection with reason "RMS: insufficient margin"
- **THEN** the order is `Rejected` with that reason, the audit trail reads Requested → Submitted → Open → Rejected, and one broker rejection is reported

#### Scenario: Rejected entry releases its reservation
- **GIVEN** an acknowledged, unfilled entry for 2 lots of an underlying whose open-lots cap is 2
- **WHEN** the broker rejects it and another entry for 2 lots of that underlying then arrives
- **THEN** the lot caps do not refuse the new entry because of the rejected one

#### Scenario: Repeat of the rejection
- **WHEN** the broker sends the same rejection again for an order that is already `Rejected`
- **THEN** the order is unchanged, the event is logged as an invalid transition, and no second rejection is reported
