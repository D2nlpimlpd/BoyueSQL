import 'dart:io';
import 'dart:math';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:dio/dio.dart';
import 'package:dio/io.dart';
import 'package:cookie_jar/cookie_jar.dart';
import 'package:dio_cookie_manager/dio_cookie_manager.dart';
import 'package:path_provider/path_provider.dart';

void main() {
  runApp(const MyApp());
}

// ================== 网络服务 ==================
class ApiService {
  static final ApiService _instance = ApiService._internal();
  late Dio dio;
  late PersistCookieJar cookieJar;
  factory ApiService() => _instance;
  
  ApiService._internal() {
    dio = Dio(BaseOptions(
      connectTimeout: const Duration(seconds: 30),
      receiveTimeout: const Duration(seconds: 120),
    ));
    
    (dio.httpClientAdapter as IOHttpClientAdapter).createHttpClient = () {
      final client = HttpClient();
      client.badCertificateCallback = (cert, host, port) => true;
      return client;
    };
  }
  
  Future<void> init() async {
    Directory appDocDir = await getApplicationDocumentsDirectory();
    cookieJar = PersistCookieJar(storage: FileStorage("${appDocDir.path}/.cookies/"));
    dio.interceptors.add(CookieManager(cookieJar));
  }
}

class MyCustomScrollBehavior extends MaterialScrollBehavior {
  @override
  Set<PointerDeviceKind> get dragDevices => { 
    PointerDeviceKind.touch, 
    PointerDeviceKind.mouse 
  };
}

class MyApp extends StatelessWidget {
  const MyApp({super.key});
  
  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: '本地智能体',
      debugShowCheckedModeBanner: false,
      scrollBehavior: MyCustomScrollBehavior(),
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF1677FF), 
          background: const Color(0xFFF0F2F5)
        ),
        useMaterial3: true,
        fontFamily: Platform.isWindows ? "Microsoft YaHei" : null,
        scaffoldBackgroundColor: const Color(0xFFF0F2F5),
      ),
      home: const LoginScreen(),
    );
  }
}

// ================== 1. 登录页面 ==================
class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});
  
  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _formKey = GlobalKey<FormState>();
  final _hostCtrl = TextEditingController(text: Platform.isAndroid ? "10.0.2.2" : "127.0.0.1");
  final _userCtrl = TextEditingController(text: "bjzx");
  final _passCtrl = TextEditingController(text: "");
  bool _isLoading = false;

  @override
  void initState() {
    super.initState();
    ApiService().init();
  }

  Future<void> _connect() async {
    if (!_formKey.currentState!.validate()) return;
    
    setState(() => _isLoading = true);
    
    final baseUrl = "http://${_hostCtrl.text}:5000";
    
    try {
      final res = await ApiService().dio.post(
        "$baseUrl/api/connect", 
        data: {
          "user": _userCtrl.text, 
          "password": _passCtrl.text, 
          "host": _hostCtrl.text, 
          "port": 1521, 
          "service_name": "oral"
        }
      );
      
      if (res.data['ok'] == true && mounted) {
        Navigator.pushReplacement(
          context, 
          MaterialPageRoute(builder: (_) => QaLayout(baseUrl: baseUrl))
        );
      } else {
        _showError(res.data['error'] ?? "连接失败");
      }
    } on DioException catch (e) {
      String errorMsg = "连接错误";
      if (e.type == DioExceptionType.connectionTimeout) {
        errorMsg = "连接超时，请检查IP地址和网络";
      } else if (e.type == DioExceptionType.receiveTimeout) {
        errorMsg = "响应超时，请检查后端服务";
      } else if (e.type == DioExceptionType.connectionError) {
        errorMsg = "无法连接到服务器，请检查后端是否启动";
      } else {
        errorMsg = "网络错误: ${e.message}";
      }
      _showError(errorMsg);
    } catch (e) {
      _showError("未知错误: $e");
    } finally {
      if (mounted) setState(() => _isLoading = false);
    }
  }

  void _showError(String msg) {
    if (!mounted) return;
    showDialog(
      context: context, 
      builder: (_) => AlertDialog(
        title: const Text("错误"), 
        content: Text(msg), 
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context), 
            child: const Text("确定")
          )
        ]
      )
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: Card(
          elevation: 0,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(8), 
            side: BorderSide(color: Colors.grey.shade300)
          ),
          child: Container(
            width: 400, 
            padding: const EdgeInsets.all(40), 
            color: Colors.white,
            child: Form(
              key: _formKey,
              child: Column(
                mainAxisSize: MainAxisSize.min, 
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                    "Oracle 智能查询", 
                    style: TextStyle(
                      fontSize: 24, 
                      fontWeight: FontWeight.bold, 
                      color: Color(0xFF1677FF)
                    )
                  ),
                  const SizedBox(height: 30),
                  
                  TextFormField(
                    controller: _hostCtrl, 
                    decoration: const InputDecoration(
                      labelText: "API IP", 
                      border: OutlineInputBorder(), 
                      isDense: true,
                      helperText: "Android模拟器请使用 10.0.2.2",
                      helperStyle: TextStyle(fontSize: 11),
                    )
                  ),
                  const SizedBox(height: 15),
                  
                  TextFormField(
                    controller: _userCtrl, 
                    decoration: const InputDecoration(
                      labelText: "用户名", 
                      border: OutlineInputBorder(), 
                      isDense: true
                    ),
                    validator: (v) => v?.isEmpty ?? true ? "用户名不能为空" : null,
                  ),
                  const SizedBox(height: 15),
                  
                  TextFormField(
                    controller: _passCtrl, 
                    obscureText: true, 
                    decoration: const InputDecoration(
                      labelText: "密码", 
                      border: OutlineInputBorder(), 
                      isDense: true
                    )
                  ),
                  const SizedBox(height: 25),
                  
                  SizedBox(
                    width: double.infinity, 
                    height: 40, 
                    child: ElevatedButton(
                      style: ElevatedButton.styleFrom(
                        backgroundColor: const Color(0xFF1677FF), 
                        foregroundColor: Colors.white, 
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(4)
                        )
                      ),
                      onPressed: _isLoading ? null : _connect,
                      child: _isLoading 
                        ? const SizedBox(
                            width: 20, 
                            height: 20, 
                            child: CircularProgressIndicator(
                              color: Colors.white, 
                              strokeWidth: 2
                            )
                          )
                        : const Text("连接数据库"),
                    )
                  )
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

// ================== 2. 主布局 ==================
class QaLayout extends StatefulWidget {
  final String baseUrl;
  const QaLayout({super.key, required this.baseUrl});
  
  @override
  State<QaLayout> createState() => _QaLayoutState();
}

class _QaLayoutState extends State<QaLayout> {
  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Row(
        children: [
          // 侧边栏
          Container(
            width: 60, 
            color: const Color(0xFF001529),
            child: Column(
              children: [
                const SizedBox(height: 20), 
                const Icon(Icons.hub, color: Colors.white, size: 30),
                const SizedBox(height: 30),
                
                IconButton(
                  icon: const Icon(Icons.search, color: Colors.white), 
                  onPressed: () {}, 
                  tooltip: "智能问答"
                ),
                
                const Spacer(),
                
                IconButton(
                  icon: const Icon(Icons.logout, color: Colors.white54), 
                  onPressed: () async {
                    // 断开连接
                    try {
                      await ApiService().dio.post("${widget.baseUrl}/api/disconnect");
                    } catch (e) {
                      // 忽略错误
                    }
                    if (mounted) {
                      Navigator.pushReplacement(
                        context, 
                        MaterialPageRoute(builder: (_) => const LoginScreen())
                      );
                    }
                  }, 
                  tooltip: "断开连接"
                ),
                const SizedBox(height: 20),
              ]
            ),
          ),
          
          // 主内容区
          Expanded(child: QaScreen(baseUrl: widget.baseUrl)),
        ],
      ),
    );
  }
}

// ================== 3. 问答页面（核心增强版）==================
class QaScreen extends StatefulWidget {
  final String baseUrl;
  const QaScreen({super.key, required this.baseUrl});
  
  @override
  State<QaScreen> createState() => _QaScreenState();
}

class _QaScreenState extends State<QaScreen> with TickerProviderStateMixin {
  final TextEditingController _qController = TextEditingController();
  final List<String> _queryHistory = [];
  
  // 🔥 新增：查询模式
  QueryMode _queryMode = QueryMode.single;
  
  bool _isLoading = false;
  String? _errorMsg;
  String? _successMsg;
  
  Map<String, dynamic>? _mainTable;
  Map<String, dynamic>? _codeTables;
  String? _sql;
  
  TabController? _tabController;
  List<TableDataInfo> _tabInfos = [];

  @override
  void dispose() {
    _tabController?.dispose();
    _qController.dispose();
    super.dispose();
  }

  Future<void> _ask() async {
    if (_qController.text.trim().isEmpty) return;
    
    // 保存历史记录
    if (!_queryHistory.contains(_qController.text)) {
      setState(() {
        _queryHistory.insert(0, _qController.text);
        if (_queryHistory.length > 20) _queryHistory.removeLast();
      });
    }
    
    setState(() { 
      _isLoading = true; 
      _errorMsg = null; 
      _successMsg = null; 
      _tabInfos = []; 
      _mainTable = null;
      _codeTables = null;
      _sql = null;
    });
    
    try {
      // 🔥 根据查询模式选择不同的API端点
      final endpoint = _queryMode == QueryMode.single 
        ? "/api/ask" 
        : "/api/ask_multi";
      
      final res = await ApiService().dio.post(
        "${widget.baseUrl}$endpoint", 
        data: { "question": _qController.text }
      );
      
      final data = res.data;
      
      if (data['ok'] == true) {
        setState(() {
          _successMsg = "✅ 查询成功${data['time_cost'] != null ? '，耗时: ${data['time_cost']}s' : ''}";
          _mainTable = data['main_table'];
          _codeTables = data['code_tables'] ?? {};
          _sql = _mainTable?['sql'];
        });
        
        _buildTabsData();
      } else {
        setState(() {
          _errorMsg = data['error'] ?? "未知错误";
          _sql = data['sql'];
        });
      }
    } on DioException catch (e) {
      String errorMsg = "请求失败";
      
      if (e.type == DioExceptionType.connectionTimeout) {
        errorMsg = "连接超时，请检查网络";
      } else if (e.type == DioExceptionType.receiveTimeout) {
        errorMsg = "查询超时，数据量可能过大";
      } else if (e.type == DioExceptionType.badResponse) {
        errorMsg = "服务器错误: ${e.response?.statusCode}";
      } else {
        errorMsg = "网络错误: ${e.message}";
      }
      
      setState(() => _errorMsg = errorMsg);
    } catch (e) {
      setState(() => _errorMsg = "未知错误: $e");
    } finally {
      if (mounted) setState(() => _isLoading = false);
    }
  }

  void _buildTabsData() {
    _tabInfos = [];
    
    // 1. 主表结果
    if (_mainTable != null && _mainTable!['rows'] != null) {
      final rows = _mainTable!['rows'] as List;
      _tabInfos.add(TableDataInfo(
        title: "主结果 (${rows.length})",
        tableName: _mainTable!['name_cn'] ?? _mainTable!['name'],
        columns: List<String>.from(_mainTable!['columns']),
        rows: List<List<dynamic>>.from(rows.map((r) => List<dynamic>.from(r))),
      ));
    }
    
    // 2. 编码表结果（🔥 保留此逻辑，即使现在可能为空）
    if (_codeTables != null && _codeTables!.isNotEmpty) {
      _codeTables!.forEach((key, val) {
        if (val['rows'] != null && (val['rows'] as List).isNotEmpty) {
          _tabInfos.add(TableDataInfo(
            title: "${val['table_cn'] ?? key} (${(val['rows'] as List).length})",
            tableName: "${val['table_cn'] ?? ''} ($key)",
            columns: List<String>.from(val['columns']),
            rows: List<List<dynamic>>.from(
              (val['rows'] as List).map((r) => List<dynamic>.from(r))
            ),
          ));
        }
      });
    }
    
    // 3. 更新TabController
    if (_tabInfos.isNotEmpty) {
      _tabController?.dispose();
      _tabController = TabController(length: _tabInfos.length, vsync: this);
      setState(() {});
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        // 顶部查询区域
        Container(
          padding: const EdgeInsets.all(20), 
          color: Colors.white,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start, 
            children: [
              // 标题栏
              Row(
                children: [
                  const Text(
                    "智能问答", 
                    style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold)
                  ),
                  const Spacer(),
                  
                  // 🔥 新增：历史记录按钮
                  if (_queryHistory.isNotEmpty)
                    PopupMenuButton<String>(
                      icon: const Icon(Icons.history, size: 20),
                      tooltip: "查询历史",
                      itemBuilder: (_) => _queryHistory.map((q) => 
                        PopupMenuItem(
                          value: q, 
                          child: SizedBox(
                            width: 300,
                            child: Text(
                              q, 
                              maxLines: 2, 
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(fontSize: 13),
                            ),
                          )
                        )
                      ).toList(),
                      onSelected: (q) => setState(() => _qController.text = q),
                    ),
                ],
              ),
              
              const SizedBox(height: 10),
              
              // 🔥 新增：查询模式切换
              Container(
                padding: const EdgeInsets.symmetric(vertical: 8),
                child: Row(
                  children: [
                    const Text(
                      "查询模式：", 
                      style: TextStyle(fontSize: 14, color: Colors.black87)
                    ),
                    const SizedBox(width: 10),
                    
                    SegmentedButton<QueryMode>(
                      segments: const [
                        ButtonSegment<QueryMode>(
                          value: QueryMode.single,
                          label: Text("单表查询", style: TextStyle(fontSize: 13)),
                          icon: Icon(Icons.table_chart, size: 16),
                        ),
                        ButtonSegment<QueryMode>(
                          value: QueryMode.multi,
                          label: Text("多表查询", style: TextStyle(fontSize: 13)),
                          icon: Icon(Icons.table_rows, size: 16),
                        ),
                      ],
                      selected: {_queryMode},
                      onSelectionChanged: (Set<QueryMode> newSelection) {
                        setState(() => _queryMode = newSelection.first);
                      },
                      style: ButtonStyle(
                        visualDensity: VisualDensity.compact,
                        tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                      ),
                    ),
                    
                    const SizedBox(width: 15),
                    
                    // 提示信息
                    Expanded(
                      child: Text(
                        _queryMode == QueryMode.single 
                          ? "💡 仅查询一张主表，自动进行编码替换" 
                          : "💡 支持多表关联查询，返回完整结果集",
                        style: TextStyle(
                          fontSize: 12, 
                          color: Colors.grey[600],
                          fontStyle: FontStyle.italic,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
              
              const SizedBox(height: 10),
              
              // 输入框和查询按钮
              Row(
                children: [
                  Expanded(
                    child: TextField(
                      controller: _qController, 
                      maxLines: 3, 
                      minLines: 1, 
                      decoration: InputDecoration(
                        hintText: _queryMode == QueryMode.single
                          ? "例如：查询EXAM_RECORD表中2025年的所有检查记录"
                          : "例如：查询EXAM_RECORD和EXAM_EENT表中2025年的所有检查记录",
                        border: const OutlineInputBorder(), 
                        contentPadding: const EdgeInsets.all(12)
                      ), 
                      onSubmitted: (_) => _ask(),
                    )
                  ),
                  
                  const SizedBox(width: 10),
                  
                  SizedBox(
                    height: 50, 
                    child: ElevatedButton.icon(
                      icon: _isLoading 
                        ? const SizedBox(
                            width: 16, 
                            height: 16, 
                            child: CircularProgressIndicator(
                              color: Colors.white, 
                              strokeWidth: 2
                            )
                          )
                        : const Icon(Icons.send, size: 18), 
                      label: const Text("查询"), 
                      style: ElevatedButton.styleFrom(
                        backgroundColor: const Color(0xFF1677FF), 
                        foregroundColor: Colors.white,
                        padding: const EdgeInsets.symmetric(horizontal: 20),
                      ), 
                      onPressed: _isLoading ? null : _ask
                    )
                  )
                ],
              ),
              
              // 状态消息
              if (_errorMsg != null) 
                Padding(
                  padding: const EdgeInsets.only(top: 8), 
                  child: Container(
                    padding: const EdgeInsets.all(10),
                    decoration: BoxDecoration(
                      color: Colors.red.shade50,
                      borderRadius: BorderRadius.circular(4),
                      border: Border.all(color: Colors.red.shade200),
                    ),
                    child: Row(
                      children: [
                        const Icon(Icons.error_outline, color: Colors.red, size: 18),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(
                            _errorMsg!, 
                            style: const TextStyle(color: Colors.red)
                          )
                        ),
                      ],
                    ),
                  )
                ),
              
              if (_successMsg != null) 
                Padding(
                  padding: const EdgeInsets.only(top: 8), 
                  child: Container(
                    padding: const EdgeInsets.all(10),
                    decoration: BoxDecoration(
                      color: Colors.green.shade50,
                      borderRadius: BorderRadius.circular(4),
                      border: Border.all(color: Colors.green.shade200),
                    ),
                    child: Row(
                      children: [
                        const Icon(Icons.check_circle_outline, color: Colors.green, size: 18),
                        const SizedBox(width: 8),
                        Text(
                          _successMsg!, 
                          style: const TextStyle(color: Colors.green)
                        ),
                      ],
                    ),
                  )
                ),
            ]
          ),
        ),
        
        const Divider(height: 1, color: Color(0xFFE0E0E0)),
        
        // SQL语句显示（可复制）
        if (_sql != null) 
          Container(
            width: double.infinity, 
            padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 8), 
            color: const Color(0xFFFAFAFA),
            child: Row(
              children: [
                Expanded(
                  child: SelectableText(
                    "SQL > $_sql", 
                    style: TextStyle(
                      fontFamily: 'monospace', 
                      fontSize: 12, 
                      color: Colors.grey[700]
                    ), 
                    maxLines: 2
                  )
                ),
                
                // 复制按钮
                IconButton(
                  icon: const Icon(Icons.copy, size: 16),
                  tooltip: "复制SQL",
                  onPressed: () {
                    Clipboard.setData(ClipboardData(text: _sql!));
                    ScaffoldMessenger.of(context).showSnackBar(
                      const SnackBar(
                        content: Text("✅ SQL已复制到剪贴板"), 
                        duration: Duration(seconds: 1)
                      )
                    );
                  },
                ),
              ],
            )
          ),
        
        // 结果展示区域
        Expanded(
          child: _tabInfos.isEmpty 
            ? Center(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(
                      Icons.search_off, 
                      size: 64, 
                      color: Colors.grey[300]
                    ),
                    const SizedBox(height: 16),
                    Text(
                      _isLoading ? "查询中..." : "暂无数据，请输入问题开始查询", 
                      style: TextStyle(color: Colors.grey[500], fontSize: 14)
                    ),
                  ],
                )
              )
            : Column(
                children: [
                  // Tab标签栏
                  Container(
                    color: Colors.white, 
                    width: double.infinity, 
                    alignment: Alignment.centerLeft, 
                    child: TabBar(
                      controller: _tabController, 
                      isScrollable: true, 
                      labelColor: const Color(0xFF1677FF), 
                      unselectedLabelColor: Colors.black54, 
                      indicatorSize: TabBarIndicatorSize.label, 
                      tabs: _tabInfos.map((e) => Tab(text: e.title)).toList()
                    )
                  ),
                  
                  // Tab内容区
                  Expanded(
                    child: Container(
                      padding: const EdgeInsets.all(10), 
                      child: TabBarView(
                        controller: _tabController, 
                        physics: const NeverScrollableScrollPhysics(), 
                        children: _tabInfos.map((info) => 
                          HighPerformanceTable(dataInfo: info)
                        ).toList()
                      )
                    )
                  )
                ],
              ),
        ),
      ],
    );
  }
}

// 🔥 新增：查询模式枚举
enum QueryMode {
  single,  // 单表查询
  multi,   // 多表查询
}

class TableDataInfo {
  final String title;
  final String tableName;
  final List<String> columns;
  final List<List<dynamic>> rows;
  
  TableDataInfo({
    required this.title, 
    required this.tableName, 
    required this.columns, 
    required this.rows
  });
}

// ================== 4. 高性能表格核心 ==================
class HighPerformanceTable extends StatefulWidget {
  final TableDataInfo dataInfo;
  final bool isFullScreen;
  
  const HighPerformanceTable({
    super.key, 
    required this.dataInfo, 
    this.isFullScreen = false
  });
  
  @override
  State<HighPerformanceTable> createState() => _HighPerformanceTableState();
}

class _HighPerformanceTableState extends State<HighPerformanceTable> {
  final ScrollController _horizontalScroll = ScrollController();
  final ScrollController _verticalScroll = ScrollController();
  
  int _currentPage = 1;
  int _pageSize = 50;
  List<double> _columnWidths = [];
  double _totalTableWidth = 0;

  @override
  void initState() {
    super.initState();
    _calculateColumnLayout();
  }

  @override
  void didUpdateWidget(covariant HighPerformanceTable oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.dataInfo != widget.dataInfo) {
      _currentPage = 1; 
      _calculateColumnLayout();
    }
  }

  @override
  void dispose() {
    _horizontalScroll.dispose();
    _verticalScroll.dispose();
    super.dispose();
  }

  void _calculateColumnLayout() {
    if (widget.dataInfo.columns.isEmpty) return;
    
    _columnWidths = widget.dataInfo.columns.map((col) {
      double width = col.length * 14.0 + 40; 
      if (width < 120) width = 120;
      if (width > 300) width = 300;
      return width;
    }).toList();
    
    _totalTableWidth = _columnWidths.reduce((a, b) => a + b);
  }

  // 🔥 新增：格式化单元格值
  String _formatCellValue(dynamic value) {
    if (value == null) return "";
    if (value is num) return value.toString();
    
    // 处理 Decimal 类型
    final str = value.toString();
    if (str.contains('Decimal')) {
      return str.replaceAll(RegExp(r"Decimal\('(.+?)'\)"), r'$1');
    }
    
    return str;
  }

  @override
  Widget build(BuildContext context) {
    int totalRows = widget.dataInfo.rows.length;
    int totalPages = (totalRows / _pageSize).ceil();
    if (totalPages == 0) totalPages = 1;
    
    int start = (_currentPage - 1) * _pageSize;
    int end = (start + _pageSize) > totalRows ? totalRows : (start + _pageSize);
    List<List<dynamic>> currentData = widget.dataInfo.rows.sublist(start, end);

    return Card(
      elevation: 0,
      color: Colors.white,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(4), 
        side: BorderSide(color: Colors.grey.shade200)
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          // 顶部工具栏
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
            child: Row(
              children: [
                Text(
                  widget.dataInfo.tableName, 
                  style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 15)
                ),
                
                const Spacer(),
                
                const Text("每页: ", style: TextStyle(fontSize: 13)),
                
                // 🔥 修复：下拉菜单样式
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 8),
                  decoration: BoxDecoration(
                    border: Border.all(color: Colors.grey.shade300),
                    borderRadius: BorderRadius.circular(4),
                  ),
                  child: DropdownButton<int>(
                    value: _pageSize,
                    underline: const SizedBox(),
                    style: const TextStyle(fontSize: 13, color: Colors.black87), 
                    dropdownColor: Colors.white,
                    items: [50, 100, 200, 500].map((e) => 
                      DropdownMenuItem(
                        value: e, 
                        child: Text("$e 条")
                      )
                    ).toList(),
                    onChanged: (v) => setState(() { 
                      _pageSize = v!; 
                      _currentPage = 1; 
                    }),
                  ),
                ),
                
                if (!widget.isFullScreen) ...[
                  const SizedBox(width: 10),
                  ElevatedButton.icon(
                    icon: const Icon(Icons.fullscreen, size: 16),
                    label: const Text("全屏"),
                    style: ElevatedButton.styleFrom(
                      backgroundColor: const Color(0xFF1677FF), 
                      foregroundColor: Colors.white, 
                      padding: const EdgeInsets.symmetric(horizontal: 10), 
                      minimumSize: const Size(0, 32)
                    ),
                    onPressed: () => Navigator.push(
                      context, 
                      MaterialPageRoute(
                        builder: (_) => FullScreenTablePage(dataInfo: widget.dataInfo)
                      )
                    ),
                  ),
                ]
              ],
            ),
          ),
          
          const Divider(height: 1, thickness: 1),

          // 表格主体区域 (高性能虚拟化)
          Expanded(
            child: Scrollbar(
              controller: _horizontalScroll,
              thumbVisibility: true,
              thickness: 12,
              radius: const Radius.circular(6),
              child: SingleChildScrollView(
                controller: _horizontalScroll,
                scrollDirection: Axis.horizontal,
                child: SizedBox(
                  width: max(
                    _totalTableWidth, 
                    MediaQuery.of(context).size.width - 100
                  ),
                  child: Column(
                    children: [
                      // 1. 固定表头
                      _buildHeaderRow(),
                      const Divider(
                        height: 1, 
                        thickness: 1, 
                        color: Color(0xFFEEEEEE)
                      ),
                      
                      // 2. 虚拟化列表区域 (垂直滚动)
                      Expanded(
                        child: Scrollbar(
                          controller: _verticalScroll,
                          thumbVisibility: true,
                          thickness: 10,
                          radius: const Radius.circular(6),
                          child: ListView.separated(
                            controller: _verticalScroll,
                            itemCount: currentData.length,
                            separatorBuilder: (ctx, i) => const Divider(
                              height: 1, 
                              thickness: 0.5, 
                              color: Color(0xFFF5F5F5)
                            ),
                            itemBuilder: (ctx, index) {
                              return _buildDataRow(currentData[index], index);
                            },
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),

          // 底部翻页栏
          Container(
            padding: const EdgeInsets.symmetric(vertical: 8),
            decoration: const BoxDecoration(
              border: Border(top: BorderSide(color: Color(0xFFEEEEEE)))
            ),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                IconButton(
                  icon: const Icon(Icons.first_page, size: 20), 
                  onPressed: _currentPage == 1 
                    ? null 
                    : () => setState(() => _currentPage = 1)
                ),
                
                IconButton(
                  icon: const Icon(Icons.chevron_left, size: 20), 
                  onPressed: _currentPage == 1 
                    ? null 
                    : () => setState(() => _currentPage--)
                ),
                
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 10), 
                  child: Text(
                    "$_currentPage / $totalPages 页 (共 $totalRows 条)", 
                    style: const TextStyle(fontSize: 13)
                  )
                ),
                
                IconButton(
                  icon: const Icon(Icons.chevron_right, size: 20), 
                  onPressed: _currentPage == totalPages 
                    ? null 
                    : () => setState(() => _currentPage++)
                ),
                
                IconButton(
                  icon: const Icon(Icons.last_page, size: 20), 
                  onPressed: _currentPage == totalPages 
                    ? null 
                    : () => setState(() => _currentPage = totalPages)
                ),
              ],
            ),
          )
        ],
      ),
    );
  }

  Widget _buildHeaderRow() {
    return Container(
      color: const Color(0xFFFAFAFA),
      height: 40,
      child: Row(
        children: List.generate(widget.dataInfo.columns.length, (index) {
          return SizedBox(
            width: _columnWidths[index],
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 10),
              child: Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  widget.dataInfo.columns[index],
                  style: const TextStyle(
                    fontWeight: FontWeight.bold, 
                    fontSize: 13, 
                    color: Colors.black87
                  ),
                  overflow: TextOverflow.ellipsis,
                ),
              ),
            ),
          );
        }),
      ),
    );
  }

  Widget _buildDataRow(List<dynamic> rowData, int index) {
    return Container(
      color: index % 2 == 0 ? Colors.white : const Color(0xFFFAFAFA),
      height: 35, 
      child: Row(
        children: List.generate(rowData.length, (colIndex) {
          if (colIndex >= _columnWidths.length) return const SizedBox();
          
          return SizedBox(
            width: _columnWidths[colIndex],
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 10),
              child: Align(
                alignment: Alignment.centerLeft,
                child: SelectableText( 
                  _formatCellValue(rowData[colIndex]),  // 🔥 使用格式化方法
                  style: const TextStyle(fontSize: 13, color: Colors.black87),
                  maxLines: 1,
                ),
              ),
            ),
          );
        }),
      ),
    );
  }
}

// ================== 5. 全屏页面 ==================
class FullScreenTablePage extends StatelessWidget {
  final TableDataInfo dataInfo;
  
  const FullScreenTablePage({super.key, required this.dataInfo});
  
  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text("📊 ${dataInfo.tableName} - 全屏浏览"),
        backgroundColor: Colors.white, 
        elevation: 1,
        iconTheme: const IconThemeData(color: Colors.black87),
        titleTextStyle: const TextStyle(
          color: Colors.black87, 
          fontSize: 18, 
          fontWeight: FontWeight.bold
        ),
      ),
      body: Container(
        color: const Color(0xFFF0F2F5), 
        padding: const EdgeInsets.all(10), 
        child: HighPerformanceTable(dataInfo: dataInfo, isFullScreen: true)
      ),
    );
  }
}