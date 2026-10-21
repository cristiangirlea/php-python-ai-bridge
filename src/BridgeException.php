<?php

declare(strict_types=1);

namespace PhpAiBridge;

class BridgeException extends \RuntimeException
{
    public function __construct(
        string $message,
        public readonly string $errorCode = 'bridge_error',
        public readonly ?int $httpStatus = null,
        /** On a 429, the worker's Retry-After in whole seconds; null when absent or not 1 to 6 digits. */
        public readonly ?int $retryAfterSeconds = null,
    ) {
        parent::__construct($message);
    }
}
