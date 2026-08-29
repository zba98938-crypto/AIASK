"""
产业链知识图谱 - 产业链关系、上下游分析
"""

from typing import List, Dict, Any, Optional, Set, Tuple
from collections import defaultdict, deque


class IndustryKnowledgeGraph:
    """产业链知识图谱"""
    
    def __init__(self):
        # 产业链数据结构
        self.nodes = {}  # {node_id: node_data}
        self.edges = defaultdict(list)  # {from_node: [(to_node, relation_type)]}
        self.reverse_edges = defaultdict(list)  # 反向边，用于查找上游
        
        # 初始化示例产业链
        self._init_sample_chains()
    
    def _init_sample_chains(self):
        """初始化示例产业链数据"""
        
        # 新能源汽车产业链
        ev_chain = {
            'nodes': [
                {'id': 'lithium_mining', 'name': '锂矿开采', 'level': 'upstream', 'sector': '有色金属'},
                {'id': 'lithium_processing', 'name': '锂盐加工', 'level': 'upstream', 'sector': '化工'},
                {'id': 'cathode_material', 'name': '正极材料', 'level': 'midstream', 'sector': '新材料'},
                {'id': 'anode_material', 'name': '负极材料', 'level': 'midstream', 'sector': '新材料'},
                {'id': 'electrolyte', 'name': '电解液', 'level': 'midstream', 'sector': '化工'},
                {'id': 'separator', 'name': '隔膜', 'level': 'midstream', 'sector': '新材料'},
                {'id': 'battery_cell', 'name': '电芯制造', 'level': 'midstream', 'sector': '电池'},
                {'id': 'battery_pack', 'name': '电池包', 'level': 'midstream', 'sector': '电池'},
                {'id': 'motor', 'name': '电机', 'level': 'midstream', 'sector': '汽车零部件'},
                {'id': 'electronic_control', 'name': '电控系统', 'level': 'midstream', 'sector': '汽车零部件'},
                {'id': 'ev_manufacturing', 'name': '整车制造', 'level': 'downstream', 'sector': '汽车'},
                {'id': 'charging_station', 'name': '充电桩', 'level': 'downstream', 'sector': '电力设备'},
            ],
            'edges': [
                ('lithium_mining', 'lithium_processing', 'supply'),
                ('lithium_processing', 'cathode_material', 'supply'),
                ('cathode_material', 'battery_cell', 'supply'),
                ('anode_material', 'battery_cell', 'supply'),
                ('electrolyte', 'battery_cell', 'supply'),
                ('separator', 'battery_cell', 'supply'),
                ('battery_cell', 'battery_pack', 'supply'),
                ('battery_pack', 'ev_manufacturing', 'supply'),
                ('motor', 'ev_manufacturing', 'supply'),
                ('electronic_control', 'ev_manufacturing', 'supply'),
                ('ev_manufacturing', 'charging_station', 'demand'),
            ]
        }
        
        # 添加到图中
        for node in ev_chain['nodes']:
            self.add_node(node['id'], node)
        
        for from_node, to_node, relation in ev_chain['edges']:
            self.add_edge(from_node, to_node, relation)
    
    # ========== 图操作 ==========
    
    def add_node(self, node_id: str, node_data: Dict[str, Any]):
        """添加节点"""
        self.nodes[node_id] = node_data
    
    def add_edge(self, from_node: str, to_node: str, relation_type: str = 'supply'):
        """添加边"""
        self.edges[from_node].append((to_node, relation_type))
        self.reverse_edges[to_node].append((from_node, relation_type))
    
    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        """获取节点"""
        return self.nodes.get(node_id)
    
    # ========== 产业链查询 ==========
    
    def get_upstream(self, node_id: str, max_depth: int = 3) -> List[Dict[str, Any]]:
        """
        获取上游节点
        
        Args:
            node_id: 节点ID
            max_depth: 最大深度
        
        Returns:
            上游节点列表
        """
        upstream_nodes = []
        visited = set()
        queue = deque([(node_id, 0)])
        
        while queue:
            current_id, depth = queue.popleft()
            
            if current_id in visited or depth >= max_depth:
                continue
            
            visited.add(current_id)
            
            # 获取上游节点
            for upstream_id, relation in self.reverse_edges.get(current_id, []):
                if upstream_id not in visited:
                    node_data = self.get_node(upstream_id)
                    if node_data:
                        upstream_nodes.append({
                            **node_data,
                            'depth': depth + 1,
                            'relation': relation,
                        })
                    queue.append((upstream_id, depth + 1))
        
        return upstream_nodes
    
    def get_downstream(self, node_id: str, max_depth: int = 3) -> List[Dict[str, Any]]:
        """
        获取下游节点
        
        Args:
            node_id: 节点ID
            max_depth: 最大深度
        
        Returns:
            下游节点列表
        """
        downstream_nodes = []
        visited = set()
        queue = deque([(node_id, 0)])
        
        while queue:
            current_id, depth = queue.popleft()
            
            if current_id in visited or depth >= max_depth:
                continue
            
            visited.add(current_id)
            
            # 获取下游节点
            for downstream_id, relation in self.edges.get(current_id, []):
                if downstream_id not in visited:
                    node_data = self.get_node(downstream_id)
                    if node_data:
                        downstream_nodes.append({
                            **node_data,
                            'depth': depth + 1,
                            'relation': relation,
                        })
                    queue.append((downstream_id, depth + 1))
        
        return downstream_nodes
    
    def get_full_chain(self, node_id: str) -> Dict[str, Any]:
        """
        获取完整产业链
        
        Args:
            node_id: 节点ID
        
        Returns:
            完整产业链
        """
        node = self.get_node(node_id)
        if not node:
            return {'error': 'Node not found'}
        
        upstream = self.get_upstream(node_id)
        downstream = self.get_downstream(node_id)
        
        return {
            'current': node,
            'upstream': upstream,
            'downstream': downstream,
            'total_nodes': 1 + len(upstream) + len(downstream),
        }
    
    # ========== 路径查找 ==========
    
    def find_path(self, start_node: str, end_node: str) -> Optional[List[str]]:
        """
        查找两个节点之间的路径（BFS）
        
        Args:
            start_node: 起始节点
            end_node: 目标节点
        
        Returns:
            路径（节点ID列表）
        """
        if start_node not in self.nodes or end_node not in self.nodes:
            return None
        
        visited = set()
        queue = deque([(start_node, [start_node])])
        
        while queue:
            current_id, path = queue.popleft()
            
            if current_id == end_node:
                return path
            
            if current_id in visited:
                continue
            
            visited.add(current_id)
            
            # 探索下游节点
            for next_id, _ in self.edges.get(current_id, []):
                if next_id not in visited:
                    queue.append((next_id, path + [next_id]))
        
        return None
    
    # ========== 关键节点识别 ==========
    
    def find_key_nodes(self) -> List[Dict[str, Any]]:
        """
        识别关键节点（度中心性高的节点）
        
        Returns:
            关键节点列表
        """
        node_degrees = {}
        
        for node_id in self.nodes:
            # 计算度（入度+出度）
            in_degree = len(self.reverse_edges.get(node_id, []))
            out_degree = len(self.edges.get(node_id, []))
            total_degree = in_degree + out_degree
            
            node_degrees[node_id] = {
                'node': self.get_node(node_id),
                'in_degree': in_degree,
                'out_degree': out_degree,
                'total_degree': total_degree,
            }
        
        # 按总度排序
        sorted_nodes = sorted(
            node_degrees.values(),
            key=lambda x: x['total_degree'],
            reverse=True
        )
        
        return sorted_nodes[:10]  # 返回前10个关键节点
    
    # ========== 产业链分析 ==========
    
    def analyze_chain(self, keyword: str) -> Dict[str, Any]:
        """
        分析产业链
        
        Args:
            keyword: 关键词
        
        Returns:
            产业链分析结果
        """
        # 查找相关节点
        related_nodes = []
        for node_id, node_data in self.nodes.items():
            if keyword in node_data.get('name', '') or keyword in node_data.get('sector', ''):
                related_nodes.append({
                    'id': node_id,
                    **node_data
                })
        
        if not related_nodes:
            return {'error': f'No nodes found for keyword: {keyword}'}
        
        # 分析每个相关节点的产业链
        chains = []
        for node in related_nodes:
            chain = self.get_full_chain(node['id'])
            chains.append(chain)
        
        # 统计
        all_sectors = set()
        for chain in chains:
            for node in chain.get('upstream', []) + [chain.get('current', {})] + chain.get('downstream', []):
                if 'sector' in node:
                    all_sectors.add(node['sector'])
        
        return {
            'keyword': keyword,
            'related_nodes': related_nodes,
            'chains': chains,
            'involved_sectors': list(all_sectors),
            'total_chains': len(chains),
        }
    
    # ========== 影响传导分析 ==========
    
    def analyze_impact_propagation(
        self,
        source_node: str,
        impact_type: str = 'positive',
        decay_factor: float = 0.8
    ) -> Dict[str, Any]:
        """
        分析影响传导
        
        Args:
            source_node: 源节点
            impact_type: 影响类型 ('positive' 或 'negative')
            decay_factor: 衰减因子
        
        Returns:
            影响传导分析
        """
        if source_node not in self.nodes:
            return {'error': 'Source node not found'}
        
        # 初始影响强度
        initial_impact = 1.0
        
        # BFS传播影响
        impacts = {source_node: initial_impact}
        visited = set()
        queue = deque([(source_node, initial_impact, 0)])
        
        while queue:
            current_id, current_impact, depth = queue.popleft()
            
            if current_id in visited:
                continue
            
            visited.add(current_id)
            
            # 向下游传播
            for next_id, relation in self.edges.get(current_id, []):
                if next_id not in visited:
                    # 影响衰减
                    next_impact = current_impact * decay_factor
                    
                    if next_id not in impacts or impacts[next_id] < next_impact:
                        impacts[next_id] = next_impact
                    
                    queue.append((next_id, next_impact, depth + 1))
        
        # 构建结果
        impact_nodes = []
        for node_id, impact_value in impacts.items():
            if node_id != source_node:
                node_data = self.get_node(node_id)
                impact_nodes.append({
                    **node_data,
                    'impact_value': float(impact_value),
                    'impact_level': 'high' if impact_value > 0.6 else 'medium' if impact_value > 0.3 else 'low',
                })
        
        # 按影响强度排序
        impact_nodes.sort(key=lambda x: x['impact_value'], reverse=True)
        
        return {
            'source_node': self.get_node(source_node),
            'impact_type': impact_type,
            'affected_nodes': impact_nodes,
            'total_affected': len(impact_nodes),
        }
    
    # ========== 瓶颈识别 ==========
    
    def identify_bottlenecks(self) -> List[Dict[str, Any]]:
        """
        识别产业链瓶颈（单点依赖）
        
        Returns:
            瓶颈节点列表
        """
        bottlenecks = []
        
        for node_id, node_data in self.nodes.items():
            # 检查下游节点的上游依赖
            downstream_nodes = self.edges.get(node_id, [])
            
            for downstream_id, _ in downstream_nodes:
                upstream_of_downstream = self.reverse_edges.get(downstream_id, [])
                
                # 如果下游节点只依赖当前节点，则当前节点是瓶颈
                if len(upstream_of_downstream) == 1:
                    bottlenecks.append({
                        'bottleneck_node': node_data,
                        'dependent_node': self.get_node(downstream_id),
                        'risk_level': 'high',
                    })
        
        return bottlenecks


# 全局实例
industry_kg = IndustryKnowledgeGraph()
