// 【修改声明】本文件修改自 Universal Web-to-API（AGPL-3.0）上游项目。
// 修改清单见 CHANGES.md，最后修改日期 2026-11-05。本程序整体仍以 AGPL-3.0 发布。

const { createApp } = Vue

// ========== 配置 Schema 定义 ==========

// 浏览器常量 Schema（纯中文显示）
const BROWSER_CONSTANTS_SCHEMA = {
    connection: {
        label: '连接配置',
        icon: '🔌',
        items: {
            DEFAULT_PORT: { 
                label: '调试端口', 
                desc: 'Chrome DevTools 远程调试端口', 
                type: 'number',
                min: 1024,
                max: 65535,
                default: 9222
            },
            CONNECTION_TIMEOUT: { 
                label: '连接超时', 
                unit: '秒',
                desc: '浏览器连接超时时间',
                type: 'number',
                min: 1,
                max: 60,
                default: 10
            }
        }
    },
    delay: {
        label: '操作延迟',
        icon: '⏱️',
        desc: '模拟人类操作的随机延迟范围',
        items: {
            STEALTH_DELAY_MIN: { 
                label: '隐身延迟下限', 
                unit: '秒', 
                type: 'number', 
                step: 0.05,
                min: 0,
                default: 0.1
            },
            STEALTH_DELAY_MAX: { 
                label: '隐身延迟上限', 
                unit: '秒', 
                type: 'number', 
                step: 0.05,
                min: 0,
                default: 0.3
            },
            ACTION_DELAY_MIN: { 
                label: '动作延迟下限', 
                unit: '秒', 
                type: 'number', 
                step: 0.05,
                min: 0,
                default: 0.15
            },
            ACTION_DELAY_MAX: { 
                label: '动作延迟上限', 
                unit: '秒', 
                type: 'number', 
                step: 0.05,
                min: 0,
                default: 0.3
            }
        }
    },
    element: {
        label: '元素查找',
        icon: '🔍',
        items: {
            DEFAULT_ELEMENT_TIMEOUT: { 
                label: '默认等待时间', 
                unit: '秒',
                desc: '查找元素的默认超时',
                type: 'number',
                min: 1,
                default: 3
            },
            FALLBACK_ELEMENT_TIMEOUT: { 
                label: '备用等待时间', 
                unit: '秒',
                desc: '首次失败后的重试超时',
                type: 'number',
                min: 0.5,
                default: 1
            },
            ELEMENT_CACHE_MAX_AGE: { 
                label: '缓存有效期', 
                unit: '秒',
                desc: '元素位置缓存时间',
                type: 'number',
                min: 1,
                default: 5.0
            }
        }
    },
    stream: {
        label: '流式监控',
        icon: '📡',
        desc: '控制 AI 响应的检测频率和超时判定',
        items: {
            STREAM_CHECK_INTERVAL_MIN: { 
                label: '检查间隔下限', 
                unit: '秒',
                type: 'number',
                step: 0.05,
                min: 0.05,
                default: 0.1
            },
            STREAM_CHECK_INTERVAL_MAX: { 
                label: '检查间隔上限', 
                unit: '秒',
                type: 'number',
                step: 0.1,
                min: 0.1,
                default: 1.0
            },
            STREAM_CHECK_INTERVAL_DEFAULT: { 
                label: '默认检查间隔', 
                unit: '秒',
                type: 'number',
                step: 0.05,
                min: 0.05,
                default: 0.3
            },
            STREAM_SILENCE_THRESHOLD: { 
                label: '静默超时阈值', 
                unit: '秒', 
                desc: '无新内容多久后判定完成',
                type: 'number',
                min: 1,
                default: 8.0
            },
            STREAM_SILENCE_THRESHOLD_FALLBACK: { 
                label: '静默超时备用', 
                unit: '秒', 
                desc: '慢速模型的备用阈值',
                type: 'number',
                min: 1,
                default: 12
            },
            STREAM_MAX_TIMEOUT: { 
                label: '最大超时', 
                unit: '秒',
                desc: '单次响应的绝对超时上限',
                type: 'number',
                min: 60,
                default: 600
            },
            STREAM_INITIAL_WAIT: { 
                label: '初始等待', 
                unit: '秒', 
                desc: '等待首次响应的最长时间',
                type: 'number',
                min: 10,
                default: 180
            },
            STREAM_STABLE_COUNT_THRESHOLD: { 
                label: '稳定判定次数', 
                desc: '连续多少次检查不变才判定完成',
                type: 'number',
                min: 1,
                default: 8
            }
        }
    },
    streamAdvanced: {
        label: '流式监控（高级）',
        icon: '⚙️',
        collapsed: true,
        items: {
            STREAM_RERENDER_WAIT: { 
                label: '重渲染等待', 
                unit: '秒',
                desc: '等待页面重新渲染',
                type: 'number',
                step: 0.1,
                default: 0.5
            },
            STREAM_CONTENT_SHRINK_TOLERANCE: { 
                label: '内容收缩容忍次数', 
                desc: '允许内容变短的次数',
                type: 'number',
                min: 0,
                default: 3
            },
            STREAM_MIN_VALID_LENGTH: { 
                label: '最小有效长度', 
                unit: '字符',
                desc: '响应被视为有效的最小长度',
                type: 'number',
                min: 1,
                default: 10
            },
            STREAM_INITIAL_ELEMENT_WAIT: { 
                label: '初始元素等待', 
                unit: '秒',
                type: 'number',
                min: 1,
                default: 10
            },
            STREAM_MAX_ABNORMAL_COUNT: { 
                label: '最大异常次数', 
                desc: '连续异常多少次后中止',
                type: 'number',
                min: 1,
                default: 5
            },
            STREAM_MAX_ELEMENT_MISSING: { 
                label: '最大元素丢失次数', 
                type: 'number',
                min: 1,
                default: 10
            },
            STREAM_CONTENT_SHRINK_THRESHOLD: { 
                label: '内容收缩阈值', 
                desc: '内容缩减超过此比例视为异常',
                type: 'number',
                step: 0.05,
                min: 0,
                max: 1,
                default: 0.3
            }
        }
    },
    validation: {
        label: '输入验证',
        icon: '✅',
        items: {
            MAX_MESSAGE_LENGTH: { 
                label: '消息最大长度', 
                unit: '字符',
                type: 'number',
                min: 1000,
                default: 100000
            },
            MAX_MESSAGES_COUNT: { 
                label: '消息最大数量', 
                unit: '条',
                type: 'number',
                min: 1,
                default: 100
            }
        }
    }
};

// 环境变量 Schema
const ENV_CONFIG_SCHEMA = {
    service: {
        label: '服务配置',
        icon: '🖥️',
        items: {
            APP_HOST: { 
                label: '监听地址', 
                desc: '0.0.0.0 允许外部访问，127.0.0.1 仅本地', 
                type: 'text',
                default: '127.0.0.1'
            },
            APP_PORT: { 
                label: '监听端口', 
                type: 'number',
                min: 1,
                max: 65535,
                default: 8199
            },
            APP_DEBUG: { 
                label: '调试模式', 
                desc: '开启 API 文档和详细错误',
                type: 'switch',
                default: true
            },
            LOG_LEVEL: { 
                label: '日志级别', 
                type: 'select', 
                options: ['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                default: 'INFO'
            }
        }
    },
    auth: {
        label: '认证配置',
        icon: '🔐',
        items: {
            AUTH_ENABLED: { 
                label: '启用认证', 
                type: 'switch',
                default: false
            },
            AUTH_TOKEN: { 
                label: 'Bearer Token', 
                type: 'password', 
                desc: 'AUTH_ENABLED=true 时必须设置',
                default: ''
            }
        }
    },
    cors: {
        label: 'CORS 配置',
        icon: '🌐',
        items: {
            CORS_ENABLED: { 
                label: '启用 CORS', 
                type: 'switch',
                default: true
            },
            CORS_ORIGINS: { 
                label: '允许的跨域源', 
                desc: '多个用逗号分隔，* 表示全部允许',
                type: 'text',
                default: '*'
            }
        }
    },
    browser: {
        label: '浏览器配置',
        icon: '🌍',
        items: {
            BROWSER_PORT: { 
                label: 'Chrome 调试端口', 
                type: 'number',
                min: 1024,
                max: 65535,
                default: 9222
            }
        }
    },
    dashboard: {
        label: 'Dashboard 配置',
        icon: '📊',
        items: {
            DASHBOARD_ENABLED: { 
                label: '启用 Dashboard', 
                type: 'switch',
                default: true
            },
            DASHBOARD_FILE: { 
                label: 'Dashboard 文件路径', 
                type: 'text',
                default: 'dashboard.html'
            }
        }
    },
    ai: {
        label: 'AI 分析配置',
        icon: '🤖',
        desc: '辅助 AI 用于自动分析页面结构',
        items: {
            HELPER_API_KEY: { 
                label: 'API Key', 
                type: 'password',
                default: ''
            },
            HELPER_BASE_URL: { 
                label: 'API 地址', 
                type: 'text',
                default: 'http://127.0.0.1:5104/v1'
            },
            HELPER_MODEL: { 
                label: '模型名称', 
                type: 'text',
                default: 'gemini-3.0-pro'
            },
            MAX_HTML_CHARS: { 
                label: 'HTML 最大字符数', 
                desc: '超过会截断以节省 Token',
                type: 'number',
                min: 10000,
                default: 120000
            }
        }
    },
    files: {
        label: '配置文件',
        icon: '📁',
        items: {
            SITES_CONFIG_FILE: { 
                label: '站点配置文件路径', 
                type: 'text',
                default: 'sites.json'
            }
        }
    }
};

// ========== Vue 应用 ==========

createApp({
    data() {
        return {
            // 数据
            sites: {},
            currentDomain: null,
            searchQuery: '',
            
            // UI 状态
            toasts: [],
            toastCounter: 0,
            isSaving: false,
            isLoading: false,
            showJsonPreview: false,
            showTokenDialog: false,
            showStepTemplates: false,
            showTestDialog: false,
            showSelectorMenu: false,
            darkMode: false,
            
            // Tab 切换（新增 settings）
            activeTab: 'config',  // 'config' | 'logs' | 'settings'
            
            // 折叠面板状态
            selectorCollapsed: false,
            workflowCollapsed: false,
            
            // 浏览器状态
            browserStatus: {
                connected: false,
                tab_url: null,
                tab_title: null
            },
            
            // 认证
            authEnabled: false,
            tempToken: '',
            
            // 选择器测试
            testSelectorInput: '',
            testTimeout: 2,
            testResult: null,
            isTesting: false,
            testHighlight: false,
            
            // 日志相关
            logs: [],
            logLevelFilter: 'ALL',
            pauseLogs: false,
            lastLogTimestamp: 0,
            logPollingTimer: null,
            
            // ========== 新增：导入功能 ==========
            showImportDialog: false,
            importMode: 'merge',  // 'merge' | 'replace'
            importedConfig: null,
            importFileName: '',
            
            // ========== 新增：系统设置 ==========
            // 环境配置
            envConfig: {},
            envConfigOriginal: {},  // 用于检测变更
            envCollapsed: {},       // 各分组折叠状态
            isSavingEnv: false,
            isLoadingEnv: false,
            
            // 浏览器常量
            browserConstants: {},
            browserConstantsOriginal: {},
            browserConstantsCollapsed: {},
            instances: [],
            configSites: [],
            newInstanceName: '',
            isCreatingInstance: false,
            isSavingConstants: false,
            isLoadingConstants: false,
            
            // Schema 引用
            envSchema: ENV_CONFIG_SCHEMA,
            browserConstantsSchema: BROWSER_CONSTANTS_SCHEMA
        }
    },

    computed: {
        // SVG 图标引用
        $icons() {
            return window.$icons || {};
        },
        
        filteredSites() {
            const keys = Object.keys(this.sites).sort()
            return this.searchQuery 
                ? keys.filter(d => d.toLowerCase().includes(this.searchQuery.toLowerCase()))
                : keys
        },
        
        currentConfig() {
            return this.currentDomain ? this.sites[this.currentDomain] : null
        },
        
        hasToken() {
            return !!localStorage.getItem('api_token')
        },
        
        // 过滤后的日志
        filteredLogs() {
            if (this.logLevelFilter === 'ALL') {
                return this.logs;
            }
            return this.logs.filter(log => log.level === this.logLevelFilter);
        },
        
        // 新增：检测环境配置是否有变更
        envConfigChanged() {
            return JSON.stringify(this.envConfig) !== JSON.stringify(this.envConfigOriginal);
        },
        
        // 新增：检测浏览器常量是否有变更
        browserConstantsChanged() {
            return JSON.stringify(this.browserConstants) !== JSON.stringify(this.browserConstantsOriginal);
        }
    },

    mounted() {
        // 读取夜间模式设置
        const savedDarkMode = localStorage.getItem('darkMode')
        if (savedDarkMode !== null) {
            this.darkMode = savedDarkMode === 'true'
        } else {
            this.darkMode = window.matchMedia('(prefers-color-scheme: dark)').matches
        }
        this.applyDarkMode()
        
        // 初始化折叠状态
        this.initCollapsedStates()
        
        this.loadConfig()
        this.refreshStatus()
        this.checkAuth()
        
        // 启动日志轮询（每 1 秒）
        this.logPollingTimer = setInterval(() => {
            this.pollLogs();
        }, 1000);
        
        // 新增：加载系统设置
        this.loadEnvConfig()
        this.loadBrowserConstants()
        this.loadInstances()
        // 实例状态轮询（15 秒，多账号面板在配置页）
        this.instancePollingTimer = setInterval(() => {
            if (this.activeTab === 'config' || this.activeTab === 'settings') this.loadInstances(true)
        }, 15000)
    },

    beforeUnmount() {
        if (this.logPollingTimer) {
            clearInterval(this.logPollingTimer);
        }
        if (this.instancePollingTimer) {
            clearInterval(this.instancePollingTimer);
        }
    },

    methods: {
        // ========== 初始化 ==========
        
        initCollapsedStates() {
            // 环境配置分组默认展开
            for (const key of Object.keys(ENV_CONFIG_SCHEMA)) {
                this.envCollapsed[key] = false;
            }
            // 浏览器常量分组，根据 schema 的 collapsed 属性决定
            for (const [key, group] of Object.entries(BROWSER_CONSTANTS_SCHEMA)) {
                this.browserConstantsCollapsed[key] = group.collapsed || false;
            }
        },
        
        // ========== 夜间模式 ==========
        
        applyDarkMode() {
            if (this.darkMode) {
                document.documentElement.classList.add('dark')
            } else {
                document.documentElement.classList.remove('dark')
            }
        },
        
        toggleDarkMode() {
            this.darkMode = !this.darkMode
            localStorage.setItem('darkMode', this.darkMode.toString())
            this.applyDarkMode()
            this.notify('已切换到' + (this.darkMode ? '夜间' : '日间') + '模式', 'success')
        },
        
        // ========== 选择器菜单 ==========
        
        toggleSelectorMenu() {
            this.showSelectorMenu = !this.showSelectorMenu
        },
        
        closeAllMenus() {
            this.showSelectorMenu = false
        },
        
        // ========== API 调用 ==========
        
        async apiRequest(url, options = {}) {
            const token = localStorage.getItem('api_token')
            const headers = {
                'Content-Type': 'application/json',
                ...options.headers
            }
            
            if (token) {
                headers['Authorization'] = 'Bearer ' + token
            }
            
            try {
                const response = await fetch(url, {
                    ...options,
                    headers
                })
                
                if (!response.ok) {
                    if (response.status === 401) {
                        this.notify('认证失败，请检查 Token', 'error')
                        this.showTokenDialog = true
                        throw new Error('未授权')
                    }
                    
                    const errorData = await response.json().catch(() => ({}))
                    throw new Error(errorData.detail || '请求失败 (' + response.status + ')')
                }
                
                return await response.json()
            } catch (error) {
                if (error.message !== '未授权') {
                    console.error('API 请求错误:', error)
                }
                throw error
            }
        },

        async loadConfig() {
            this.isLoading = true
            try {
                const data = await this.apiRequest('/api/config')
                this.sites = this.normalizeConfig(data)
                
                if (!this.currentDomain && Object.keys(this.sites).length > 0) {
                    this.currentDomain = Object.keys(this.sites)[0]
                }
            } catch (error) {
                this.notify('加载配置失败: ' + error.message, 'error')
                this.sites = {}
            } finally {
                this.isLoading = false
            }
        },

        async saveConfig() {
            if (!this.validateConfig()) {
                return
            }
            
            this.isSaving = true
            try {
                await this.apiRequest('/api/config', {
                    method: 'POST',
                    body: JSON.stringify({ config: this.sites })
                })
                this.notify('配置已保存', 'success')
            } catch (error) {
                this.notify('保存失败: ' + error.message, 'error')
            } finally {
                this.isSaving = false
            }
        },

        async refreshStatus() {
            try {
                const health = await this.apiRequest('/health')
                this.browserStatus = health.browser || {}
                this.authEnabled = health.config?.auth_enabled || false
            } catch (error) {
                console.error('状态检查失败:', error)
            }
        },

        async checkAuth() {
            try {
                const health = await this.apiRequest('/health')
                this.authEnabled = health.config?.auth_enabled || false
            } catch (error) {
                if (error.message === '未授权') {
                    this.authEnabled = true
                }
            }
        },

        async testSelector(key, selector) {
            if (!selector) {
                this.notify('选择器为空', 'warning')
                return
            }
            
            this.testSelectorInput = selector
            this.showTestDialog = true
            this.testResult = null
            
            await this.runTest()
        },

        async runTest() {
            if (!this.testSelectorInput) return
            
            this.isTesting = true
            this.testResult = null
            
            try {
                const result = await this.apiRequest('/api/debug/test-selector', {
                    method: 'POST',
                    body: JSON.stringify({
                        selector: this.testSelectorInput,
                        timeout: this.testTimeout,
                        highlight: this.testHighlight
                    })
                })
                
                this.testResult = result
                
                if (result.success) {
                    if (result.count > 1) {
                        this.notify('✅ 找到 ' + result.count + ' 个元素' + (this.testHighlight ? '，已全部高亮' : ''), 'success')
                    } else {
                        this.notify('✅ 选择器有效' + (this.testHighlight ? '，已高亮显示' : ''), 'success')
                    }
                } else {
                    this.notify('❌ 选择器无效', 'error')
                }
            } catch (error) {
                this.testResult = {
                    success: false,
                    message: error.message
                }
                this.notify('测试失败: ' + error.message, 'error')
            } finally {
                this.isTesting = false
            }
        },

        async testCurrentSite() {
            if (!this.currentConfig || Object.keys(this.currentConfig.selectors).length === 0) {
                this.notify('当前站点没有选择器', 'warning')
                return
            }
            
            this.notify('开始批量测试...', 'info')
            
            let successCount = 0
            let failCount = 0
            
            for (const [key, selector] of Object.entries(this.currentConfig.selectors)) {
                if (!selector) continue
                
                try {
                    const result = await this.apiRequest('/api/debug/test-selector', {
                        method: 'POST',
                        body: JSON.stringify({
                            selector: selector,
                            timeout: 2
                        })
                    })
                    
                    if (result.success) {
                        successCount++
                        console.log('✅ ' + key + ': ' + selector)
                    } else {
                        failCount++
                        console.warn('❌ ' + key + ': ' + selector)
                    }
                } catch (error) {
                    failCount++
                    console.error('❌ ' + key + ': ' + error.message)
                }
            }
            
            this.notify('测试完成: ' + successCount + ' 成功, ' + failCount + ' 失败', 
                       failCount > 0 ? 'warning' : 'success')
        },

        async reanalyzeCurrentSite() {
            if (!this.currentDomain) return
            
            if (!confirm('确定要删除 ' + this.currentDomain + ' 的配置并重新分析吗？\n\n重新分析需要浏览器当前正在访问该站点。')) {
                return
            }
            
            try {
                await this.apiRequest('/api/config/' + this.currentDomain, {
                    method: 'DELETE'
                })
                
                this.notify('配置已删除，请刷新页面让 AI 重新分析', 'info')
                
                delete this.sites[this.currentDomain]
                this.currentDomain = null
            } catch (error) {
                this.notify('删除失败: ' + error.message, 'error')
            }
        },

        // ========== 日志相关 ==========
        
        async pollLogs() {
            if (this.pauseLogs) return;
            
            try {
                const result = await this.apiRequest('/api/logs?since=' + this.lastLogTimestamp);
                
                if (result.logs && result.logs.length > 0) {
                    result.logs.forEach(log => {
                        this.logs.push({
                            id: Date.now() + Math.random(),
                            timestamp: new Date(log.timestamp * 1000).toLocaleTimeString() + '.' + 
                                      String(Math.floor((log.timestamp % 1) * 1000)).padStart(3, '0'),
                            level: this.parseLogLevel(log.message),
                            message: log.message
                        });
                    });
                    
                    if (this.logs.length > 500) {
                        this.logs = this.logs.slice(-500);
                    }
                    
                    this.$nextTick(() => {
                        if (this.$refs.logContainer) {
                            this.$refs.logContainer.scrollTop = this.$refs.logContainer.scrollHeight;
                        }
                    });
                    
                    this.lastLogTimestamp = result.timestamp;
                }
            } catch (error) {
                console.debug('日志轮询失败:', error.message);
            }
        },
        
        parseLogLevel(message) {
            if (message.includes('[AI]') || message.includes('AI')) return 'AI';
            if (message.includes('[ERROR]') || message.includes('ERROR')) return 'ERROR';
            if (message.includes('[WARN]') || message.includes('WARNING')) return 'WARN';
            if (message.includes('[OK]') || message.includes('[SUCCESS]') || message.includes('✅')) return 'OK';
            return 'INFO';
        },
        
        getLogColorClass(level) {
            const colors = {
                'INFO': 'bg-gray-50 dark:bg-gray-900',
                'AI': 'bg-purple-50 dark:bg-purple-900/20',
                'OK': 'bg-green-50 dark:bg-green-900/20',
                'WARN': 'bg-yellow-50 dark:bg-yellow-900/20',
                'ERROR': 'bg-red-50 dark:bg-red-900/20'
            };
            return colors[level] || colors['INFO'];
        },
        
        getLogLevelClass(level) {
            const colors = {
                'INFO': 'text-gray-600 dark:text-gray-400',
                'AI': 'text-purple-600 dark:text-purple-400',
                'OK': 'text-green-600 dark:text-green-400',
                'WARN': 'text-yellow-600 dark:text-yellow-400',
                'ERROR': 'text-red-600 dark:text-red-400'
            };
            return colors[level] || colors['INFO'];
        },
        
        clearLogs() {
            if (confirm('确定清除所有日志吗？')) {
                this.logs = [];
                this.lastLogTimestamp = Date.now() / 1000;
                
                this.apiRequest('/api/logs', { method: 'DELETE' })
                    .catch(() => {});
                
                this.notify('日志已清除', 'success');
            }
        },

        // ========== 新增：导入功能 ==========
        
        triggerImport() {
            // 触发隐藏的文件输入框
            this.$refs.importFileInput.click();
        },
        
        handleImportFile(event) {
            const file = event.target.files[0];
            if (!file) return;
            
            this.importFileName = file.name;
            
            const reader = new FileReader();
            reader.onload = (e) => {
                try {
                    const config = JSON.parse(e.target.result);
                    
                    // 验证格式
                    if (!this.validateImportedConfig(config)) {
                        this.notify('导入文件格式无效', 'error');
                        return;
                    }
                    
                    this.importedConfig = config;
                    this.showImportDialog = true;
                } catch (error) {
                    this.notify('JSON 解析失败: ' + error.message, 'error');
                }
            };
            reader.readAsText(file);
            
            // 清空 input，允许重复选择同一文件
            event.target.value = '';
        },
        
        validateImportedConfig(config) {
            // 基本验证：必须是对象
            if (typeof config !== 'object' || config === null || Array.isArray(config)) {
                return false;
            }
            
            // 检查每个站点的结构
            for (const [domain, siteConfig] of Object.entries(config)) {
                if (typeof siteConfig !== 'object') return false;
                
                // selectors 必须是对象
                if (siteConfig.selectors && typeof siteConfig.selectors !== 'object') {
                    return false;
                }
                
                // workflow 必须是数组
                if (siteConfig.workflow && !Array.isArray(siteConfig.workflow)) {
                    return false;
                }
            }
            
            return true;
        },
        
        async executeImport() {
            if (!this.importedConfig) return;
            
            const importCount = Object.keys(this.importedConfig).length;
            
            if (this.importMode === 'replace') {
                // 替换模式：完全替换
                this.sites = this.normalizeConfig(this.importedConfig);
            } else {
                // 合并模式：已存在的站点会被覆盖
                const normalized = this.normalizeConfig(this.importedConfig);
                this.sites = { ...this.sites, ...normalized };
            }
            
            // 保存到服务器
            try {
                await this.apiRequest('/api/config', {
                    method: 'POST',
                    body: JSON.stringify({ config: this.sites })
                });
                
                this.notify('成功导入 ' + importCount + ' 个站点配置', 'success');
            } catch (error) {
                this.notify('保存失败: ' + error.message, 'error');
            }
            
            // 清理
            this.showImportDialog = false;
            this.importedConfig = null;
            this.importFileName = '';
            
            // 选中第一个站点
            if (!this.currentDomain && Object.keys(this.sites).length > 0) {
                this.currentDomain = Object.keys(this.sites)[0];
            }
        },
        
        cancelImport() {
            this.showImportDialog = false;
            this.importedConfig = null;
            this.importFileName = '';
        },

        // ========== 新增：环境配置 ==========
        
        async loadEnvConfig() {
            this.isLoadingEnv = true;
            try {
                const data = await this.apiRequest('/api/settings/env');
                this.envConfig = data.config || {};
                this.envConfigOriginal = JSON.parse(JSON.stringify(this.envConfig));
            } catch (error) {
                console.error('加载环境配置失败:', error);
                // 使用 Schema 默认值
                this.envConfig = this.getEnvDefaults();
                this.envConfigOriginal = JSON.parse(JSON.stringify(this.envConfig));
            } finally {
                this.isLoadingEnv = false;
            }
        },
        
        getEnvDefaults() {
            const defaults = {};
            for (const group of Object.values(ENV_CONFIG_SCHEMA)) {
                for (const [key, field] of Object.entries(group.items)) {
                    defaults[key] = field.default;
                }
            }
            return defaults;
        },
        
        async saveEnvConfig() {
            this.isSavingEnv = true;
            try {
                await this.apiRequest('/api/settings/env', {
                    method: 'POST',
                    body: JSON.stringify({ config: this.envConfig })
                });
                
                this.envConfigOriginal = JSON.parse(JSON.stringify(this.envConfig));
                this.notify('环境配置已保存（部分配置需重启生效）', 'success');
            } catch (error) {
                this.notify('保存失败: ' + error.message, 'error');
            } finally {
                this.isSavingEnv = false;
            }
        },
        
        resetEnvConfig() {
            if (!confirm('确定要重置环境配置为默认值吗？')) return;
            
            this.envConfig = this.getEnvDefaults();
            this.notify('已重置为默认值，请点击保存以应用', 'info');
        },
        
        // ========== 新增：浏览器常量 ==========
        
        async loadBrowserConstants() {
            this.isLoadingConstants = true;
            try {
                const data = await this.apiRequest('/api/settings/browser-constants');
                this.browserConstants = data.config || {};
                this.browserConstantsOriginal = JSON.parse(JSON.stringify(this.browserConstants));
            } catch (error) {
                console.error('加载浏览器常量失败:', error);
                // 使用 Schema 默认值
                this.browserConstants = this.getBrowserConstantsDefaults();
                this.browserConstantsOriginal = JSON.parse(JSON.stringify(this.browserConstants));
            } finally {
                this.isLoadingConstants = false;
            }
        },
        
        getBrowserConstantsDefaults() {
            const defaults = {};
            for (const group of Object.values(BROWSER_CONSTANTS_SCHEMA)) {
                for (const [key, field] of Object.entries(group.items)) {
                    defaults[key] = field.default;
                }
            }
            return defaults;
        },

        // ========== 多账号实例 ==========

        async loadInstances(silent = false) {
            try {
                const data = await this.apiRequest('/api/instances');
                this.instances = data.instances || [];
                this.configSites = data.sites || [];
                if (!silent) this.instancesLoaded = true;
            } catch (error) {
                if (!silent) this.notify('加载多账号实例失败: ' + error.message, 'error');
            }
        },

        async createInstance() {
            this.isCreatingInstance = true;
            try {
                const data = await this.apiRequest('/api/instances', {
                    method: 'POST',
                    body: JSON.stringify({ name: this.newInstanceName.trim() })
                });
                this.notify(data.message || '浏览器窗口已启动，请在弹出的窗口里登录账号',
                    data.window_visible === false ? 'error' : 'success');
                this.newInstanceName = '';
                await this.loadInstances(true);
            } catch (error) {
                this.notify('新增账号失败: ' + error.message, 'error');
            } finally {
                this.isCreatingInstance = false;
            }
        },

        async launchInstance(inst, takeOver) {
            try {
                // takeOver：端口已通但窗口开在服务会话里（看不见、没法登录），
                // 让后端结束旧进程并在当前用户桌面重开一个
                const q = takeOver ? '?take_over=true' : '';
                const data = await this.apiRequest(`/api/instances/${encodeURIComponent(inst.name)}/launch${q}`, { method: 'POST' });
                this.notify(data.message || `已启动 ${inst.name}`,
                    data.window_visible === false ? 'error' : 'success');
                await this.loadInstances(true);
            } catch (error) {
                this.notify('启动失败: ' + error.message, 'error');
            }
        },

        async deleteInstance(inst) {
            if (!confirm(`确定删除账号 "${inst.name}"？将关闭其浏览器窗口并移除配置（登录数据保留）。`)) return;
            try {
                await this.apiRequest(`/api/instances/${encodeURIComponent(inst.name)}`, { method: 'DELETE' });
                this.notify(`账号 ${inst.name} 已移除`, 'success');
                await this.loadInstances(true);
            } catch (error) {
                this.notify('删除失败: ' + error.message, 'error');
            }
        },

        async clearCreditRecord(inst, cs) {
            try {
                let url = `/api/credit-state?domain=${encodeURIComponent(cs.domain)}&port=${inst.port}`;
                // toolong / limited 是另两类记录，清除接口靠 kind 区分
                const kind = (cs.kind === 'toolong' || cs.kind === 'limited')
                    ? cs.kind : '';
                if (kind) {
                    url += `&kind=${kind}`;
                }
                // 指定模型只清该模型记录；不传清该账号该站点全部模型
                if (cs.model) {
                    url += `&model=${encodeURIComponent(cs.model)}`;
                }
                await this.apiRequest(url, { method: 'DELETE' });
                const what = kind === 'toolong' ? '消息过长标记'
                    : kind === 'limited' ? '受限冷却' : '积分记录';
                this.notify(`${inst.name} 的 ${cs.domain}${cs.model ? ' · ' + cs.model : ''} ${what}已清除，该账号立即可用`, 'success');
                await this.loadInstances(true);
            } catch (error) {
                this.notify('清除失败: ' + error.message, 'error');
            }
        },

        fmtResetTime(ts) {
            try {
                return new Date(ts * 1000).toLocaleTimeString('zh-CN',
                    { hour: '2-digit', minute: '2-digit', hour12: false });
            } catch (e) {
                return '每日重置';
            }
        },

        async saveBrowserConstants() {
            this.isSavingConstants = true;
            try {
                await this.apiRequest('/api/settings/browser-constants', {
                    method: 'POST',
                    body: JSON.stringify({ config: this.browserConstants })
                });
                
                this.browserConstantsOriginal = JSON.parse(JSON.stringify(this.browserConstants));
                this.notify('浏览器常量已保存', 'success');
            } catch (error) {
                this.notify('保存失败: ' + error.message, 'error');
            } finally {
                this.isSavingConstants = false;
            }
        },
        
        resetBrowserConstants() {
            if (!confirm('确定要重置浏览器常量为默认值吗？')) return;
            
            this.browserConstants = this.getBrowserConstantsDefaults();
            this.notify('已重置为默认值，请点击保存以应用', 'info');
        },
        
        // ========== 数据操作 ==========
        
        normalizeConfig(raw) {
            const norm = {}
            for (const [k, v] of Object.entries(raw)) {
                norm[k] = {
                    selectors: v.selectors || {},
                    workflow: v.workflow || [],
                    stealth: !!v.stealth
                }
            }
            return norm
        },

        validateConfig() {
            if (!this.currentDomain || !this.currentConfig) {
                this.notify('请选择站点', 'warning')
                return false
            }
            
            const selectors = this.currentConfig.selectors
            if (Object.keys(selectors).length === 0) {
                this.notify('至少需要一个选择器', 'warning')
                return false
            }
            
            const workflow = this.currentConfig.workflow
            for (let i = 0; i < workflow.length; i++) {
                const step = workflow[i]
                
                if (!step.action) {
                    this.notify('步骤 ' + (i + 1) + ': 缺少动作类型', 'error')
                    return false
                }
                
                if (['FILL_INPUT', 'CLICK', 'STREAM_WAIT'].includes(step.action)) {
                    if (!step.target) {
                        this.notify('步骤 ' + (i + 1) + ': 请选择目标选择器', 'error')
                        return false
                    }
                }
                
                if (step.action === 'KEY_PRESS' && !step.target) {
                    this.notify('步骤 ' + (i + 1) + ': 请输入按键名称', 'error')
                    return false
                }
                
                if (step.action === 'WAIT' && (!step.value || step.value <= 0)) {
                    this.notify('步骤 ' + (i + 1) + ': 等待时间必须大于 0', 'error')
                    return false
                }
            }
            
            return true
        },

        selectSite(domain) {
            this.currentDomain = domain
        },

        addNewSite() {
            const domain = prompt('请输入域名（例如: chat.example.com）:')
            if (!domain) return
            
            if (this.sites[domain]) {
                this.notify('该站点已存在', 'warning')
                this.currentDomain = domain
                return
            }
            
            this.sites[domain] = {
                selectors: {},
                workflow: [],
                stealth: false
            }
            this.currentDomain = domain
            this.notify('已创建站点: ' + domain, 'success')
        },

        confirmDelete(domain) {
            if (!confirm('确定要删除 ' + domain + ' 的配置吗？')) {
                return
            }
            
            delete this.sites[domain]
            
            if (this.currentDomain === domain) {
                this.currentDomain = Object.keys(this.sites)[0] || null
            }
            
            this.notify('已删除: ' + domain, 'info')
        },

        // ========== 选择器操作 ==========
        
        addSelector(preset) {
            this.showSelectorMenu = false
            
            let key
            if (preset === 'custom') {
                key = prompt('请输入选择器名称（例如: input_box）')
                if (!key) return
            } else {
                key = preset
            }
            
            if (this.currentConfig.selectors[key]) {
                this.notify('选择器 "' + key + '" 已存在', 'warning')
                return
            }
            
            this.currentConfig.selectors[key] = ''
            this.notify('已添加选择器: ' + key, 'success')
        },

        removeSelector(key) {
            if (!confirm('确定删除选择器 ' + key + ' 吗？')) {
                return
            }
            
            delete this.currentConfig.selectors[key]
            
            this.currentConfig.workflow.forEach(function(step) {
                if (step.target === key) {
                    step.target = ''
                }
            })
        },

        updateSelectorKey(oldKey, newKey) {
            if (!newKey || oldKey === newKey) return
            
            newKey = newKey.trim()
            
            if (this.currentConfig.selectors[newKey]) {
                this.notify('该键名已存在', 'error')
                return
            }
            
            this.currentConfig.selectors[newKey] = this.currentConfig.selectors[oldKey]
            delete this.currentConfig.selectors[oldKey]
            
            this.currentConfig.workflow.forEach(function(step) {
                if (step.target === oldKey) {
                    step.target = newKey
                }
            })
        },

        // ========== 工作流操作 ==========
        
        addStep() {
            const defaultStep = {
                action: 'CLICK',
                target: '',
                optional: false,
                value: null
            }
            
            this.currentConfig.workflow.push(defaultStep)
        },

        removeStep(index) {
            this.currentConfig.workflow.splice(index, 1)
        },

        moveStep(index, direction) {
            const arr = this.currentConfig.workflow
            const newIndex = index + direction
            
            if (newIndex < 0 || newIndex >= arr.length) return
            
            const temp = arr[index]
            arr[index] = arr[newIndex]
            arr[newIndex] = temp
        },

        onActionChange(step) {
            if (['FILL_INPUT', 'CLICK', 'STREAM_WAIT'].includes(step.action)) {
                step.value = null
                if (!step.target) step.target = ''
            } else if (step.action === 'KEY_PRESS') {
                step.value = null
                if (!step.target) step.target = 'Enter'
            } else if (step.action === 'WAIT') {
                step.target = ''
                if (!step.value) step.value = '1.0'
            }
        },

        applyTemplate(type) {
            const templates = {
                'default': [
                    { action: 'CLICK', target: 'new_chat_btn', optional: true, value: null },
                    { action: 'WAIT', target: '', optional: false, value: '0.5' },
                    { action: 'FILL_INPUT', target: 'input_box', optional: false, value: null },
                    { action: 'CLICK', target: 'send_btn', optional: true, value: null },
                    { action: 'KEY_PRESS', target: 'Enter', optional: true, value: null },
                    { action: 'STREAM_WAIT', target: 'result_container', optional: false, value: null }
                ],
                'simple': [
                    { action: 'FILL_INPUT', target: 'input_box', optional: false, value: null },
                    { action: 'KEY_PRESS', target: 'Enter', optional: false, value: null },
                    { action: 'STREAM_WAIT', target: 'result_container', optional: false, value: null }
                ]
            }
            
            if (!confirm('这将覆盖当前的工作流配置，确定继续吗？')) {
                return
            }
            
            this.currentConfig.workflow = JSON.parse(JSON.stringify(templates[type]))
            this.showStepTemplates = false
            this.notify('模板已应用', 'success')
        },

        // ========== 工具功能 ==========
        
        exportConfig() {
            const dataStr = JSON.stringify(this.sites, null, 2)
            const blob = new Blob([dataStr], { type: 'application/json' })
            const url = URL.createObjectURL(blob)
            const a = document.createElement('a')
            a.href = url
            a.download = 'sites-config-' + Date.now() + '.json'
            a.click()
            URL.revokeObjectURL(url)
            
            this.notify('配置已导出', 'success')
        },

        copyJson() {
            const text = JSON.stringify(this.sites[this.currentDomain], null, 2)
            navigator.clipboard.writeText(text).then(() => {
                this.notify('已复制到剪贴板', 'success')
            }).catch(() => {
                this.notify('复制失败', 'error')
            })
        },

        saveToken() {
            if (this.tempToken.trim()) {
                localStorage.setItem('api_token', this.tempToken.trim())
                this.notify('Token 已保存', 'success')
            } else {
                localStorage.removeItem('api_token')
                this.notify('Token 已清除', 'info')
            }
            
            this.showTokenDialog = false
            this.tempToken = ''
            
            this.loadConfig()
        },

        // ========== Toast 通知 ==========
        
        notify(message, type) {
            if (!type) type = 'info'
            const id = this.toastCounter++
            this.toasts.push({ id: id, message: message, type: type })
            
            const self = this
            setTimeout(function() {
                self.removeToast(id)
            }, 3000)
        },

        removeToast(id) {
            this.toasts = this.toasts.filter(function(t) {
                return t.id !== id
            })
        }
    }
}).mount('#app')