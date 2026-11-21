package com.example.chatroom;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.DhcpInfo;
import android.net.Uri;
import android.net.wifi.WifiManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.InputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;

/**
 * 聊天室 App 的主界面。
 *
 * 它本身只是个“外壳”：用一个全屏 WebView 加载电脑上跑着的聊天室网页，
 * 所以网页有的功能（已读、撤回、编辑、未读、历史……）在 App 里全都有。
 *
 * 三种界面状态：
 *   - 正在连接：转圈 + 显示当前地址（不是白屏）
 *   - 聊天室：全屏网页
 *   - 连不上：错误页，显示地址、排查提示、重试/改地址按钮
 */
public class MainActivity extends Activity {

    private static final String PREFS = "chatroom";
    private static final String KEY_SERVER = "server_url";
    private static final int LOAD_TIMEOUT_MS = 12000;
    private static final int DISCOVERY_PORT = 9999;
    private static final String DISCOVERY_MAGIC = "CHATROOM_DISCOVER_V1";
    private static final int REQ_FILE = 1001;

    private WebView webView;
    private View contentBox;
    private View loadingView;
    private View errorView;
    private TextView loadingUrlText;
    private TextView errorUrlText;
    private SharedPreferences prefs;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private String serverUrl = "";

    private boolean isLoading = false;
    /** 主框架加载失败过（Chromium 的错误页也会触发 onPageFinished，用它避免盖掉自己的错误页） */
    private boolean mainFrameError = false;
    /** 本次打开 App 是否已经自动查找过一次，避免失败后无限重试 */
    private boolean autoDiscoveryTried = false;
    private Runnable timeoutRunnable;
    /** 网页里 <input type="file"> 的回调，选完图要还给它 */
    private ValueCallback<Uri[]> filePathCallback;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        webView = (WebView) findViewById(R.id.webview);
        contentBox = findViewById(R.id.content_box);
        loadingView = findViewById(R.id.loading_view);
        errorView = findViewById(R.id.error_view);
        loadingUrlText = (TextView) findViewById(R.id.loading_url);
        errorUrlText = (TextView) findViewById(R.id.error_url);

        setupWebView();

        findViewById(R.id.retry_btn).setOnClickListener(v -> loadServer());
        findViewById(R.id.discover_btn).setOnClickListener(v -> discoverServer(null, true));
        findViewById(R.id.change_btn).setOnClickListener(v -> showServerDialog(false));

        serverUrl = prefs.getString(KEY_SERVER, "");
        if (serverUrl == null || serverUrl.isEmpty()) {
            showServerDialog(true);
        } else {
            loadServer();
        }
    }

    private void setupWebView() {
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        // 必须开启：网页用 localStorage 记昵称/设备号、sessionStorage 记窗口号
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String url = request.getUrl().toString();
                if (!serverUrl.isEmpty() && url.startsWith(serverUrl)) {
                    return false;  // 聊天室自己的页面，交给 WebView 加载
                }
                // 其它链接（正常情况下没有）交给系统浏览器
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                } catch (Exception ignored) {
                }
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (mainFrameError) {
                    // 这是 Chromium 自己的错误页，别盖掉我们的错误界面
                    mainFrameError = false;
                    return;
                }
                showContent();
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request.isForMainFrame()) {
                    mainFrameError = true;
                    onLoadFailed();
                }
            }

            @Override
            public void onReceivedHttpError(WebView view, WebResourceRequest request,
                                            WebResourceResponse errorResponse) {
                // 服务器能连上但返回 404/500（比如地址写错端口、static 文件丢了）
                if (request.isForMainFrame() && errorResponse != null
                        && errorResponse.getStatusCode() >= 400) {
                    showError();
                }
            }
        });

        // 网页里的「发送照片 / 换头像」用的是 <input type="file">。
        // WebView 必须实现 onShowFileChooser，否则点了完全没反应。
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (filePathCallback != null) {
                    filePathCallback.onReceiveValue(null);
                }
                filePathCallback = callback;
                try {
                    Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
                    String type = "image/*";
                    String[] accept = params.getAcceptTypes();
                    if (accept != null && accept.length > 0 && accept[0] != null) {
                        String candidate = accept[0].trim();
                        if (candidate.contains("/")) {
                            type = candidate;
                        }
                    }
                    intent.setType(type);
                    intent.addCategory(Intent.CATEGORY_OPENABLE);
                    startActivityForResult(Intent.createChooser(intent, "选择图片"), REQ_FILE);
                } catch (Exception e) {
                    filePathCallback = null;
                    Toast.makeText(MainActivity.this, "打不开文件选择器", Toast.LENGTH_SHORT).show();
                    return false;
                }
                return true;
            }
        });
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_FILE) {
            return;
        }
        if (filePathCallback == null) {
            return;
        }
        Uri[] results = null;
        if (resultCode == RESULT_OK) {
            results = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
        }
        filePathCallback.onReceiveValue(results);
        filePathCallback = null;
    }

    @Override
    protected void onDestroy() {
        if (filePathCallback != null) {
            filePathCallback.onReceiveValue(null);
            filePathCallback = null;
        }
        super.onDestroy();
    }

    private void loadServer() {
        if (serverUrl.isEmpty()) {
            showServerDialog(true);
            return;
        }
        mainFrameError = false;
        showLoading();
        webView.loadUrl(serverUrl);
        scheduleTimeout();
    }

    private void scheduleTimeout() {
        cancelTimeout();
        isLoading = true;
        timeoutRunnable = () -> {
            if (isLoading) {
                webView.stopLoading();
                onLoadFailed();
            }
        };
        mainHandler.postDelayed(timeoutRunnable, LOAD_TIMEOUT_MS);
    }

    private void cancelTimeout() {
        if (timeoutRunnable != null) {
            mainHandler.removeCallbacks(timeoutRunnable);
            timeoutRunnable = null;
        }
    }

    private void showLoading() {
        isLoading = true;
        errorView.setVisibility(View.GONE);
        contentBox.setVisibility(View.VISIBLE);
        loadingView.setVisibility(View.VISIBLE);
        loadingUrlText.setText(serverUrl);
    }

    private void showContent() {
        isLoading = false;
        cancelTimeout();
        loadingView.setVisibility(View.GONE);
        errorView.setVisibility(View.GONE);
        contentBox.setVisibility(View.VISIBLE);
    }

    private void showError() {
        isLoading = false;
        cancelTimeout();
        loadingView.setVisibility(View.GONE);
        contentBox.setVisibility(View.GONE);
        errorView.setVisibility(View.VISIBLE);
        errorUrlText.setText(serverUrl.isEmpty() ? "" : "当前地址：" + serverUrl);
    }

    /**
     * 加载失败：先显示错误页，然后自动在局域网里找一次服务器。
     * 这样从寝室（手机热点）换到教室 WiFi，打开 App 就能自己接上，不用手动改地址。
     */
    private void onLoadFailed() {
        showError();
        if (!autoDiscoveryTried) {
            autoDiscoveryTried = true;
            discoverServer(null, true);
        }
    }

    /** 服务器地址输入框。firstRun=true 时不能取消，否则直接退出 App */
    private void showServerDialog(final boolean firstRun) {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setHint("例如 172.20.10.13:9999");
        input.setText(serverUrl.replace("http://", "").replace("https://", ""));
        input.setSelection(input.getText().length());

        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(pad, pad / 2, pad, 0);
        box.addView(input);

        // 输入框下面放一排小按钮：自动查找 / 测试连接
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);

        Button discoverBtn = new Button(this);
        discoverBtn.setText("自动查找");
        discoverBtn.setAllCaps(false);
        discoverBtn.setOnClickListener(v -> discoverServer(input, false));

        Button testBtn = new Button(this);
        testBtn.setText("测试连接");
        testBtn.setAllCaps(false);
        testBtn.setOnClickListener(v ->
                testConnection(normalize(input.getText().toString())));

        LinearLayout.LayoutParams half = new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        row.addView(discoverBtn, half);
        row.addView(testBtn, half);
        box.addView(row);

        AlertDialog.Builder builder = new AlertDialog.Builder(this)
                .setTitle("连接聊天室服务器")
                .setMessage("点「自动查找」让 App 自己找服务器；也可以手动填黑窗口里「局域网访问」那一行的地址。\n手机和电脑要连同一个 WiFi。")
                .setView(box)
                .setPositiveButton("保存并连接", (dialog, which) -> {
                    String url = normalize(input.getText().toString());
                    if (url.isEmpty()) {
                        showServerDialog(firstRun);
                        return;
                    }
                    prefs.edit().putString(KEY_SERVER, url).apply();
                    serverUrl = url;
                    loadServer();
                })
                .setNegativeButton(firstRun ? "退出" : "取消", (dialog, which) -> {
                    if (firstRun) {
                        finish();
                    }
                });

        AlertDialog dialog = builder.create();
        dialog.setCancelable(!firstRun);
        dialog.show();
    }

    /** 在后台线程请求一下 /state，用来确认地址是通的 */
    private void testConnection(final String url) {
        if (url.isEmpty()) {
            Toast.makeText(this, "请先填写地址", Toast.LENGTH_SHORT).show();
            return;
        }
        Toast.makeText(this, "正在测试 " + url + " …", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            boolean ok = false;
            String detail = "";
            HttpURLConnection conn = null;
            try {
                conn = (HttpURLConnection) new URL(url + "/state").openConnection();
                conn.setConnectTimeout(3000);
                conn.setReadTimeout(3000);
                int code = conn.getResponseCode();
                ok = code == 200;
                detail = "HTTP " + code;
                InputStream in = ok ? conn.getInputStream() : conn.getErrorStream();
                if (in != null) {
                    in.close();
                }
            } catch (Exception e) {
                detail = e.getClass().getSimpleName();
            } finally {
                if (conn != null) {
                    conn.disconnect();
                }
            }
            final boolean success = ok;
            final String reason = detail;
            mainHandler.post(() -> Toast.makeText(
                    MainActivity.this,
                    success ? "连接成功，可以点「保存并连接」了"
                            : "连接失败（" + reason + "）：检查手机和电脑是否同一 WiFi、"
                              + "电脑上的聊天室窗口是否开着、地址是否电脑当前 IP",
                    Toast.LENGTH_LONG).show());
        }).start();
    }

    /**
     * 在局域网里广播一句「谁是聊天室服务器」，服务器会回自己的端口。
     * 这样电脑换了 IP 也不用去改地址。
     */
    private void discoverServer(final EditText targetInput, final boolean autoConnect) {
        Toast.makeText(this, "正在局域网内查找服务器…", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            String found = null;
            DatagramSocket socket = null;
            try {
                socket = new DatagramSocket();
                socket.setBroadcast(true);
                socket.setSoTimeout(1500);
                byte[] payload = DISCOVERY_MAGIC.getBytes("UTF-8");

                List<InetAddress> targets = new ArrayList<>();
                targets.add(InetAddress.getByName("255.255.255.255"));
                InetAddress subnet = subnetBroadcast();
                if (subnet != null && !targets.contains(subnet)) {
                    targets.add(subnet);
                }
                for (InetAddress target : targets) {
                    for (int i = 0; i < 2; i++) {
                        socket.send(new DatagramPacket(payload, payload.length,
                                target, DISCOVERY_PORT));
                    }
                }

                byte[] buf = new byte[512];
                long deadline = System.currentTimeMillis() + 4000;
                while (System.currentTimeMillis() < deadline) {
                    DatagramPacket packet = new DatagramPacket(buf, buf.length);
                    try {
                        socket.receive(packet);
                    } catch (SocketTimeoutException e) {
                        break;
                    }
                    String body = new String(packet.getData(), 0, packet.getLength(), "UTF-8");
                    if (!body.contains("chatroom")) {
                        continue;
                    }
                    int port = DISCOVERY_PORT;
                    try {
                        port = new JSONObject(body).optInt("port", DISCOVERY_PORT);
                    } catch (Exception ignored) {
                    }
                    found = "http://" + packet.getAddress().getHostAddress() + ":" + port;
                    break;
                }
            } catch (Exception e) {
                // 忽略，统一走下面的失败提示
            } finally {
                if (socket != null) {
                    socket.close();
                }
            }

            final String result = found;
            mainHandler.post(() -> {
                if (result == null) {
                    Toast.makeText(this,
                            "没找到服务器：确认电脑黑窗口开着、手机和电脑连同一个网络、"
                                    + "防火墙放行 UDP " + DISCOVERY_PORT,
                            Toast.LENGTH_LONG).show();
                    return;
                }
                serverUrl = result;
                prefs.edit().putString(KEY_SERVER, result).apply();
                if (targetInput != null) {
                    targetInput.setText(result.replace("http://", ""));
                    targetInput.setSelection(targetInput.getText().length());
                }
                Toast.makeText(this, "找到服务器：" + result, Toast.LENGTH_SHORT).show();
                if (autoConnect) {
                    loadServer();
                }
            });
        }).start();
    }

    /** 按当前 WiFi 的 IP 推算 /24 网段广播地址（255.255.255.255 被路由器拦时的备用） */
    private InetAddress subnetBroadcast() {
        try {
            WifiManager wm = (WifiManager) getApplicationContext()
                    .getSystemService(Context.WIFI_SERVICE);
            if (wm == null) {
                return null;
            }
            DhcpInfo info = wm.getDhcpInfo();
            if (info == null || info.ipAddress == 0) {
                return null;
            }
            int broadcast = (info.ipAddress & 0x00FFFFFF) | 0xFF000000;
            byte[] bytes = new byte[] {
                    (byte) (broadcast & 0xFF),
                    (byte) ((broadcast >> 8) & 0xFF),
                    (byte) ((broadcast >> 16) & 0xFF),
                    (byte) ((broadcast >> 24) & 0xFF),
            };
            return InetAddress.getByAddress(bytes);
        } catch (Exception e) {
            return null;
        }
    }

    private String normalize(String raw) {
        String s = raw == null ? "" : raw.trim();
        if (s.isEmpty()) {
            return "";
        }
        if (!s.startsWith("http://") && !s.startsWith("https://")) {
            s = "http://" + s;
        }
        while (s.endsWith("/")) {
            s = s.substring(0, s.length() - 1);
        }
        return s;
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        getMenuInflater().inflate(R.menu.main, menu);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        int id = item.getItemId();
        if (id == R.id.action_settings) {
            showServerDialog(false);
            return true;
        }
        if (id == R.id.action_reload) {
            loadServer();
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && webView.canGoBack()) {
            webView.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }
}
