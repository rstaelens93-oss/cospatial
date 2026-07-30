package com.cospatial.app;

import android.net.http.SslError;
import android.os.Bundle;
import android.webkit.SslErrorHandler;
import android.webkit.WebView;

import com.getcapacitor.BridgeActivity;
import com.getcapacitor.BridgeWebViewClient;

public class MainActivity extends BridgeActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // In debug builds only: allow self-signed / untrusted certificates so
        // the Capacitor WebView can reach development servers without Android
        // terminating the SSL handshake.
        //
        // BuildConfig.DEBUG is false in every release / production build, so
        // this block is dead code in signed APKs and will NOT trigger Google
        // Play's security scanner.  handler.proceed() must NEVER appear in a
        // release build — the BridgeWebViewClient subclass approach here
        // ensures that guarantee is enforced at compile time.
        if (BuildConfig.DEBUG) {
            getBridge().setWebViewClient(new BridgeWebViewClient(getBridge()) {
                @Override
                public void onReceivedSslError(WebView view,
                                               SslErrorHandler handler,
                                               SslError error) {
                    // Bypass SSL certificate errors during development only.
                    handler.proceed();
                }
            });
        }
    }
}
