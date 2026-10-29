<?php

declare(strict_types=1);

namespace PhpAiBridge;

/** @internal Distinguishes an expired transport timer without exposing transport details. */
final class TransportTimeout extends BridgeException
{
    public function __construct()
    {
        parent::__construct('Bridge transport failed; submission outcome may be unknown', 'transport_error');
    }
}
