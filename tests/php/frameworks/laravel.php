<?php

declare(strict_types=1);

// Boots a real Laravel application with no skeleton: the provider must come in through package discovery from
// vendor/composer/installed.json and its configuration through vendor:publish, never a register() call here.

require __DIR__ . '/support.php';

use Illuminate\Contracts\Config\Repository;
use Illuminate\Contracts\Console\Kernel;
use Illuminate\Foundation\Application;
use PhpAiBridge\Client;
use PhpAiBridge\Laravel\BridgeServiceProvider;

/** A base path in tmpfs with the directories a booted application writes to. */
function basePath(): string
{
    $base = '/tmp/laravel-' . bin2hex(random_bytes(6));
    foreach (['bootstrap/cache', 'config', 'storage/logs', 'storage/framework/cache', 'storage/framework/views'] as $dir) {
        if (!mkdir($base . '/' . $dir, 0700, true)) {
            throw new RuntimeException('Cannot create ' . $dir);
        }
    }
    return $base;
}

function laravelApp(string $base): Application
{
    $app = Application::configure(basePath: $base)->create();
    try {
        $app->make(Kernel::class)->bootstrap();
    } finally {
        promoteWarnings();
    }
    return $app;
}

function laravelClient(Application $app): Client
{
    return $app->make(Client::class);
}

$token = token();
$vendor = (string) getenv('COMPOSER_VENDOR_DIR');
$library = '/work/src/Laravel/config/ai_bridge.php';

// Discovery reads the library's metadata from the frameworks lock, so it must match the composer.json it copies.
$installed = json_decode((string) file_get_contents($vendor . '/composer/installed.json'), true, 512, JSON_THROW_ON_ERROR);
$own = json_decode((string) file_get_contents('/work/composer.json'), true, 512, JSON_THROW_ON_ERROR);
$entry = null;
foreach (is_array($installed) && is_array($installed['packages'] ?? null) ? $installed['packages'] : [] as $package) {
    if (is_array($package) && ($package['name'] ?? null) === 'cristiangirlea/php-python-ai-bridge') {
        $entry = $package;
    }
}
if (!is_array($entry) || !is_array($own)) {
    throw new RuntimeException('The library is not in installed.json');
}
foreach (['extra', 'autoload', 'require'] as $key) {
    // Composer sorts some maps when it writes a lock, so the order of keys is not compared.
    check(($entry[$key] ?? null) == ($own[$key] ?? null),
        "tests/php/frameworks/composer.lock carries the library's {$key} as composer.json has it now: lock it again after changing it");
}

// Discovery registers the provider, and vendor:publish copies the configuration into the application.
$base = basePath();
$app = laravelApp($base);
check($app->getProviders(BridgeServiceProvider::class) !== [], 'package discovery registers BridgeServiceProvider');
$manifest = require $base . '/bootstrap/cache/packages.php';
$listed = is_array($manifest) && is_array($manifest['cristiangirlea/php-python-ai-bridge'] ?? null)
    ? $manifest['cristiangirlea/php-python-ai-bridge']['providers'] ?? null : null;
check($listed === [BridgeServiceProvider::class], 'the package manifest lists the provider under the library');
check($app->make(Kernel::class)->call('vendor:publish', ['--tag' => 'ai-bridge-config']) === 0, 'vendor:publish succeeds for the ai-bridge-config tag');
check(is_file($base . '/config/ai_bridge.php') && file_get_contents($base . '/config/ai_bridge.php') === file_get_contents($library),
    'vendor:publish copies config/ai_bridge.php into the application');

// A fresh boot with the published configuration resolves one client that reaches the worker.
$app = laravelApp($base);
$client = laravelClient($app);
check(laravelClient($app) === $client, 'a second resolution returns the same client');
rerankRoundTrip($client, 'Laravel');

// What config:cache writes: the whole configuration, read back without the environment.
$cached = '<?php return ' . var_export($app->make(Repository::class)->all(), true) . ';' . PHP_EOL;
check(file_put_contents($base . '/bootstrap/cache/config.php', $cached) !== false, 'the configuration cache is written');
setEnv('BRIDGE_URL', 'http://unused.invalid:1');
setEnv('BRIDGE_TOKEN', null);
$app = laravelApp($base);
check($app->configurationIsCached(), 'the second application boots from cached configuration');
rerankRoundTrip(laravelClient($app), 'Laravel with cached configuration');
setEnv('BRIDGE_URL', 'http://worker:8090');
setEnv('BRIDGE_TOKEN', $token);

// Invalid settings fail at first resolution, naming the setting and never quoting the token.
$short = 'short-secret-value-' . bin2hex(random_bytes(4));
$cases = [
    'missing URL' => [['BRIDGE_URL' => null], ['ai_bridge.url', 'BRIDGE_URL']],
    'empty URL' => [['BRIDGE_URL' => ''], ['ai_bridge.url', 'BRIDGE_URL']],
    'URL with a path' => [['BRIDGE_URL' => 'http://worker:8090/api'], ['ai_bridge.url', 'BRIDGE_URL']],
    'missing token' => [['BRIDGE_TOKEN' => null], ['ai_bridge.token', 'BRIDGE_TOKEN']],
    'empty token' => [['BRIDGE_TOKEN' => ''], ['ai_bridge.token', 'BRIDGE_TOKEN']],
    'short token' => [['BRIDGE_TOKEN' => $short], ['ai_bridge.token', 'BRIDGE_TOKEN']],
    'timeout 0' => [['BRIDGE_REQUEST_TIMEOUT_MS' => '0'], ['ai_bridge.request_timeout_ms', 'BRIDGE_REQUEST_TIMEOUT_MS']],
    'timeout 300001' => [['BRIDGE_REQUEST_TIMEOUT_MS' => '300001'], ['ai_bridge.request_timeout_ms', 'BRIDGE_REQUEST_TIMEOUT_MS']],
    'timeout not a number' => [['BRIDGE_REQUEST_TIMEOUT_MS' => '5s'], ['ai_bridge.request_timeout_ms', 'BRIDGE_REQUEST_TIMEOUT_MS']],
];
foreach ($cases as $case => [$env, $names]) {
    foreach ($env as $name => $value) {
        setEnv($name, $value);
    }
    $invalid = basePath();
    copy($library, $invalid . '/config/ai_bridge.php');
    $app = laravelApp($invalid);
    check($app->hasBeenBootstrapped(), 'Laravel, ' . $case . ': the application boots before the client is resolved');
    failsNaming(static fn () => laravelClient($app), $names, [$token, $short], 'Laravel, ' . $case);
    setEnv('BRIDGE_URL', 'http://worker:8090');
    setEnv('BRIDGE_TOKEN', $token);
    setEnv('BRIDGE_REQUEST_TIMEOUT_MS', null);
}

// Without a published file the provider's own defaults apply, and the environment's timeout is read as a number.
setEnv('BRIDGE_REQUEST_TIMEOUT_MS', '5000');
$app = laravelApp(basePath());
rerankRoundTrip(laravelClient($app), 'Laravel without a published configuration');
setEnv('BRIDGE_REQUEST_TIMEOUT_MS', null);

echo 'Laravel ' . $app->version() . ': ' . passed() . " checks passed\n";
