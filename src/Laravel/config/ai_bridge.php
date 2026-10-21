<?php

declare(strict_types=1);

// The environment is read here only, so `php artisan config:cache` keeps working.
return [
    'url' => env('BRIDGE_URL'),
    'token' => env('BRIDGE_TOKEN'),
    'request_timeout_ms' => env('BRIDGE_REQUEST_TIMEOUT_MS', 2000),
];
