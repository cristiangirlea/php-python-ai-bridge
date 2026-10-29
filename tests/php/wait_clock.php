<?php

declare(strict_types=1);

namespace PhpAiBridge;

final class WaitClock
{
    public static ?int $now = null;
}

// Advance polling time deterministically without depending on the host scheduler.
/** @return ($asNumber is true ? int|float : array{int, int}) */
function hrtime(bool $asNumber = false): array|int|float
{
    if ($asNumber && WaitClock::$now !== null) {
        return WaitClock::$now;
    }
    return \hrtime($asNumber);
}
