<?php

declare(strict_types=1);

// Repository examples only. Applications should use Composer's vendor/autoload.php.
spl_autoload_register(static function (string $class): void {
    $prefix = 'PhpAiBridge\\';
    if (str_starts_with($class, $prefix)) {
        $path = dirname(__DIR__) . '/src/' . substr($class, strlen($prefix)) . '.php';
        if (is_file($path)) {
            require $path;
        }
    }
});
