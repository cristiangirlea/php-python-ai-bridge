<?php

declare(strict_types=1);

// Shared by laravel.php and symfony.php, which run offline against the frameworks php-frameworks-fetcher installed.

use PhpAiBridge\BridgeException;
use PhpAiBridge\Client;
use PhpAiBridge\RerankResult;

/**
 * A framework installs its own error and exception handlers while it boots, so each test puts these back afterwards:
 * a warning fails, and an uncaught failure reaches PHP's own handler and a non-zero exit.
 */
function promoteWarnings(): void
{
    set_exception_handler(null);
    ini_set('display_errors', 'stderr');
    error_reporting(E_ALL);
    set_error_handler(static function (int $severity, string $message, string $file, int $line): bool {
        if (!(error_reporting() & $severity)) {
            return false;
        }
        throw new ErrorException($message, 0, $severity, $file, $line);
    });
}

promoteWarnings();

$vendor = getenv('COMPOSER_VENDOR_DIR') ?: throw new RuntimeException('Set COMPOSER_VENDOR_DIR to the frameworks vendor directory');
require $vendor . '/autoload.php';

$count = 0;

function check(bool $value, string $message): void
{
    /** @var int $count */
    global $count;
    if (!$value) {
        throw new RuntimeException($message);
    }
    ++$count;
}

function passed(): int
{
    /** @var int $count */
    global $count;
    return $count;
}

function token(): string
{
    $token = getenv('BRIDGE_TOKEN');
    if (!is_string($token) || strlen($token) < 32) {
        throw new RuntimeException('Set BRIDGE_TOKEN');
    }
    return $token;
}

/** Sets or removes a variable everywhere both frameworks read the environment from. */
function setEnv(string $name, ?string $value): void
{
    if ($value === null) {
        putenv($name);
        unset($_ENV[$name], $_SERVER[$name]);
        return;
    }
    putenv($name . '=' . $value);
    $_ENV[$name] = $value;
    $_SERVER[$name] = $value;
}

/**
 * The action must fail with a message that names every one of $names, and no message or trace of the chain may hold
 * any of $secrets. A trace prints at most the first 15 characters of a string argument, so that prefix must stay out
 * of the outer trace too.
 *
 * @param list<string> $names
 * @param list<string> $secrets
 */
function failsNaming(callable $action, array $names, array $secrets, string $case): void
{
    try {
        $action();
    } catch (Throwable $error) {
        $messages = '';
        $traces = '';
        for ($cause = $error; $cause !== null; $cause = $cause->getPrevious()) {
            $messages .= $cause->getMessage() . "\n";
            $traces .= $cause->getTraceAsString() . "\n";
        }
        foreach ($names as $name) {
            check(str_contains($error->getMessage(), $name), $case . ': the message names ' . $name . ', got: ' . $error->getMessage());
        }
        foreach ($secrets as $secret) {
            check(!str_contains($messages, $secret), $case . ': the token is not in the message');
            check(!str_contains($error->getTraceAsString(), substr($secret, 0, 15)), $case . ': the token is not in the trace');
            check(!str_contains($traces, $secret), $case . ': the token is not in any trace of the chain');
        }
        return;
    }
    throw new RuntimeException($case . ': expected a failure');
}

function ready(Client $client): void
{
    $deadline = microtime(true) + 15;
    while (true) {
        try {
            $client->get(str_repeat('0', 32));
        } catch (BridgeException $error) {
            if ($error->httpStatus === 404) {
                return;
            }
            if (microtime(true) > $deadline) {
                throw $error;
            }
            usleep(50000);
        }
    }
}

/** A rerank through the real worker whose best match is known. */
function rerankRoundTrip(Client $client, string $case): void
{
    ready($client);
    $documents = ['no matching words', 'needle token', 'other text'];
    $job = $client->submitRerank('needle token', $documents);
    $result = RerankResult::fromJob($client->wait($job->id), count($documents));
    check($result->rankings[0]['index'] === 1, $case . ': the worker ranks the matching document first');
}
