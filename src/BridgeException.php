<?php

declare(strict_types=1);

namespace PhpAiBridge;

class BridgeException extends \RuntimeException
{
    public function __construct(
        string $message,
        public readonly string $errorCode = 'bridge_error',
        public readonly ?int $httpStatus = null,
    ) {
        parent::__construct($message);
    }
}
