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

// The Laravel provider and the Symfony bundle ship in the library, but neither framework may come with it.
$vendors = array_values(array_diff(scandir(getcwd() . '/vendor') ?: [], ['.', '..', 'autoload.php', 'composer', 'bin']));
if ($vendors !== ['cristiangirlea']) {
    throw new RuntimeException('Installing the library alone brought in other vendors: ' . implode(', ', $vendors));
}

$packageDir = getcwd() . '/vendor/cristiangirlea/php-python-ai-bridge';
if (is_dir('/work/.git')) {
    if (!is_file($packageDir . '/src/Client.php') || !is_dir($packageDir . '/docs')) {
        throw new RuntimeException('The installed archive is missing src/Client.php or docs/');
    }
    foreach (['worker', 'tests', 'docker'] as $excluded) {
        if (file_exists($packageDir . '/' . $excluded)) {
            throw new RuntimeException('The installed archive must not contain ' . $excluded . '/');
        }
    }
    $entries = array_values(array_diff(scandir($packageDir) ?: [], ['.', '..']));
    sort($entries);
    if ($entries !== ['CHANGELOG.md', 'LICENSE', 'README.md', 'composer.json', 'docs', 'src']) {
        throw new RuntimeException('Unexpected archive entries: ' . implode(', ', $entries));
    }
} elseif (is_file('/work/.git')) {
    if ((string) getenv('BRIDGE_REQUIRE_ARCHIVE') !== '') {
        throw new RuntimeException('BRIDGE_REQUIRE_ARCHIVE is set, but /work/.git is a worktree file');
    }
    echo "consumer install: archive check skipped (/work/.git is a worktree file whose git directory is outside the mount)\n";
} else {
    throw new RuntimeException('/work/.git is neither a directory nor a worktree file');
}

echo "consumer install: PhpAiBridge\\Client autoloaded from vendor/, MAX_DOCUMENTS = 512\n";
