<?php

declare(strict_types=1);

namespace PhpAiBridge;

final readonly class TransportResponse
{
    public function __construct(
        public int $status,
        public ?string $contentType,
        public string $body,
    ) {
    }
}
