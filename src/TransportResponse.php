<?php

declare(strict_types=1);

namespace PhpAiBridge;

final readonly class TransportResponse
{
    public function __construct(
        public int $status,
        public ?string $contentType,
        public string $body,
        /** The response's Retry-After in whole seconds: exactly one header of 1 to 6 digits, else null. */
        public ?int $retryAfterSeconds = null,
    ) {
    }
}
