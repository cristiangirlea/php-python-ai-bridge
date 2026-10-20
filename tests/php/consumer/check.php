<?php

declare(strict_types=1);

// Runs in a consumer project that installed the library through Composer: the autoloader it generated, not
// examples/bootstrap.php, must load the client. A warning or deprecation on the way is a failure.
error_reporting(E_ALL);
set_error_handler(static function (int $severity, string $message, string $file, int $line): bool {
    if (!(error_reporting() & $severity)) {
        return false;
    }
    throw new ErrorException($message, 0, $severity, $file, $line);
});

$loader = require getcwd() . '/vendor/autoload.php';

$file = $loader->findFile(PhpAiBridge\Client::class);
if (!is_string($file) || !str_contains($file, '/vendor/') || !str_ends_with($file, 'cristiangirlea/php-python-ai-bridge/src/Client.php')) {
    throw new RuntimeException('The consumer autoloader does not map PhpAiBridge\\Client into vendor/: ' . var_export($file, true));
}
$client = new PhpAiBridge\Client('http://localhost:8090', str_repeat('x', 32));
if (PhpAiBridge\Client::MAX_DOCUMENTS !== 512) {
    throw new RuntimeException('Client::MAX_DOCUMENTS is not 512');
}
echo "consumer install: PhpAiBridge\\Client autoloaded from vendor/, MAX_DOCUMENTS = 512\n";
