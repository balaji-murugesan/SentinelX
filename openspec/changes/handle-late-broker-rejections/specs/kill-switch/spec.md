# Spec Delta

## ADDED Requirements

### Requirement: A rejected exit is placed again after the chase interval
When the broker rejects a flatten's exit, whether at placement or after acknowledging it, the engine SHALL place a new exit for the position's current quantity at the current price once the chase interval since the rejected exit was placed has passed, and not before. A rejection SHALL NOT count as a chase step, so the flatten keeps trying for as long as the position is open in market hours.

#### Scenario: Exit rejected after the acknowledgement
- **GIVEN** a kill is flattening a long position of 65 and the chase interval is 5 s
- **WHEN** the exit placed at 10:00:00 is acknowledged, then rejected by the broker at 10:00:01
- **THEN** no exit is placed before 10:00:05, and at the first look from 10:00:05 a new exit for 65 is placed at the current price

#### Scenario: Rejections do not use up the chase steps
- **GIVEN** the maximum number of chase steps is 3
- **WHEN** the broker rejects four exits in a row for the same position
- **THEN** a fifth exit is still placed after the chase interval
